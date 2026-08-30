/**
 * SOM WhatsApp Gateway — Baileys + Express.
 *
 * Contrato HTTP (todas las rutas exigen header  x-api-key: <API_KEY>):
 *   GET    /health
 *   GET    /sessions                          → lista de sesiones y estado
 *   POST   /sessions/:id/start               → inicia/reanuda sesión (genera QR si no hay credenciales)
 *   GET    /sessions/:id/status              → {status, phone, qr (dataURL|null), qr_at}
 *   DELETE /sessions/:id                     → logout + borra credenciales
 *   POST   /sessions/:id/check               {phone}            → {exists, jid}
 *   POST   /sessions/:id/send                {to, text}         → {id, jid}
 *   POST   /sessions/:id/send-media          {to, caption, mimetype, filename, base64} → {id, jid}
 *
 * Webhooks hacia Odoo (POST JSON a WEBHOOK_URL con header x-webhook-token):
 *   {type:'connection', session, status, phone}
 *   {type:'message', session, id, from, jid, text, timestamp, has_media, mimetype, filename, base64?}
 *   {type:'status', session, id, jid, status: 'sent'|'delivered'|'read'|'played'|'failed'}
 */
import express from "express";
import pino from "pino";
import axios from "axios";
import QRCode from "qrcode";
import fs from "fs";
import path from "path";
import { Boom } from "@hapi/boom";
import makeWASocket, {
  useMultiFileAuthState,
  DisconnectReason,
  fetchLatestBaileysVersion,
  downloadMediaMessage,
  makeCacheableSignalKeyStore,
  proto,
} from "@whiskeysockets/baileys";

const PORT = parseInt(process.env.PORT || "3000", 10);
const API_KEY = process.env.API_KEY || "";
const WEBHOOK_URL = process.env.WEBHOOK_URL || "";
const WEBHOOK_TOKEN = process.env.WEBHOOK_TOKEN || "";
const SESSIONS_DIR = process.env.SESSIONS_DIR || "/data/sessions";
const DEFAULT_CC = process.env.DEFAULT_COUNTRY_CODE || "52";
const DOWNLOAD_MEDIA = (process.env.DOWNLOAD_MEDIA || "1") === "1";

const log = pino({ level: process.env.LOG_LEVEL || "info" });
if (!API_KEY) { log.error("API_KEY vacío: define API_KEY en .env"); process.exit(1); }
fs.mkdirSync(SESSIONS_DIR, { recursive: true });

// ───────────────────────────── sesiones ─────────────────────────────
const sessions = new Map(); // id → {sock, status, phone, qr, qrAt, starting}

async function webhook(payload) {
  if (!WEBHOOK_URL) return;
  try {
    await axios.post(WEBHOOK_URL, payload, {
      headers: { "x-webhook-token": WEBHOOK_TOKEN, "content-type": "application/json" },
      timeout: 15000,
    });
  } catch (e) {
    log.warn({ err: e.message, type: payload.type }, "webhook falló");
  }
}

function sessionState(id) {
  const s = sessions.get(id);
  if (!s) return { session: id, status: "stopped", phone: null, qr: null, qr_at: null };
  return { session: id, status: s.status, phone: s.phone || null, qr: s.qr || null, qr_at: s.qrAt || null };
}

// Ids ya entregados a Odoo (memoria acotada) para no duplicar webhooks.
const seenIds = new Set();
function rememberId(id) {
  if (!id) return;
  seenIds.add(id);
  if (seenIds.size > 5000) { const first = seenIds.values().next().value; seenIds.delete(first); }
}

function normalizePhone(raw) {
  let d = String(raw || "").replace(/\D/g, "");
  if (!d) return "";
  if (d.length === 10) d = DEFAULT_CC + d;              // MX local → 52XXXXXXXXXX
  if (d.startsWith("521") && d.length === 13) d = "52" + d.slice(3); // 521… legado → 52…
  return d;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
// Ritmo humano: "escribiendo…" proporcional al largo del texto antes de enviar.
async function humanTyping(sock, jid, text) {
  try {
    await sock.presenceSubscribe(jid);
    await sock.sendPresenceUpdate("composing", jid);
    const ms = Math.min(1000 + (text || "").length * 35, 6000) + Math.floor(Math.random() * 1500);
    await sleep(ms);
    await sock.sendPresenceUpdate("paused", jid);
  } catch (e) { /* la presencia es cosmética: nunca bloquea el envío */ }
}

async function resolveJid(sock, to) {
  if (String(to).includes("@")) return String(to);
  const phone = normalizePhone(to);
  if (!phone) throw new Error("Número vacío");
  const res = await sock.onWhatsApp(phone);
  const hit = Array.isArray(res) ? res.find((r) => r.exists) : null;
  if (!hit) throw new Error(`El número ${phone} no tiene WhatsApp`);
  return hit.jid;
}

// ── Almacén persistente de mensajes ENVIADOS (por sesión) ──
// WhatsApp pide reintentos cuando el receptor no pudo descifrar; sin
// `getMessage` el reintento no se puede atender y el teléfono muestra
// "Esperando el mensaje. Esto puede tomar tiempo" para siempre.
const SENT_STORE_MAX = 3000;
function makeSentStore(dir) {
  const file = path.join(dir, "sent-store.json");
  let data = {};
  try { data = JSON.parse(fs.readFileSync(file, "utf8")); } catch (e) { data = {}; }
  let timer = null;
  const persist = () => {
    if (timer) return;
    timer = setTimeout(() => {
      timer = null;
      try { fs.writeFileSync(file, JSON.stringify(data)); } catch (e) { log.warn({ err: e.message }, "sent-store no guardado"); }
    }, 500);
  };
  return {
    put(id, message) {
      if (!id || !message) return;
      try { data[id] = { t: Date.now(), m: Buffer.from(proto.Message.encode(message).finish()).toString("base64") }; } catch (e) { return; }
      const keys = Object.keys(data);
      if (keys.length > SENT_STORE_MAX) {
        keys.sort((a, b) => data[a].t - data[b].t).slice(0, keys.length - SENT_STORE_MAX).forEach((k) => delete data[k]);
      }
      persist();
    },
    get(id) {
      const e = id && data[id];
      if (!e) return undefined;
      try { return proto.Message.decode(Buffer.from(e.m, "base64")); } catch (e2) { return undefined; }
    },
  };
}
// Contador de reintentos por mensaje (interfaz CacheStore de Baileys).
function makeRetryCache() {
  const m = new Map();
  return { get: (k) => m.get(k), set: (k, v) => m.set(k, v), del: (k) => m.delete(k), flushAll: () => m.clear() };
}

function sessionOptionsFile(id) { return path.join(SESSIONS_DIR, id, "options.json"); }
function loadSessionOptions(id) {
  try { return JSON.parse(fs.readFileSync(sessionOptionsFile(id), "utf8")); } catch (e) { return {}; }
}

async function startSession(id, opts) {
  const existing = sessions.get(id);
  if (existing && (existing.status === "connected" || existing.starting)) return existing;

  const dir = path.join(SESSIONS_DIR, id);
  fs.mkdirSync(dir, { recursive: true });
  const { state, saveCreds } = await useMultiFileAuthState(dir);
  const sentStore = makeSentStore(dir);
  const { version } = await fetchLatestBaileysVersion().catch(() => ({ version: undefined }));

  const entry = existing || { status: "starting", phone: null, qr: null, qrAt: null };
  entry.sentStore = sentStore;
  // Opciones por sesión (persisten para el arranque automático). mark_read
  // solo en el número genérico: en el teléfono de un vendedor NO marcamos
  // leídos sus chats.
  const options = Object.assign({ mark_read: true }, loadSessionOptions(id), opts || {});
  try { fs.mkdirSync(path.join(SESSIONS_DIR, id), { recursive: true }); fs.writeFileSync(sessionOptionsFile(id), JSON.stringify(options)); } catch (e) { /* opcional */ }
  entry.options = options;
  entry.starting = true;
  entry.status = "starting";
  sessions.set(id, entry);

  const sock = makeWASocket({
    version,
    printQRInTerminal: false,
    auth: { creds: state.creds, keys: makeCacheableSignalKeyStore(state.keys, log) },
    logger: pino({ level: "silent" }),
    browser: ["SOM Odoo", "Chrome", "1.0"],
    syncFullHistory: false,
    markOnlineOnConnect: false,
    // Reintentos: Baileys pide aquí el mensaje original cuando el receptor no
    // pudo descifrarlo; sin esto el receptor ve "Esperando el mensaje…".
    getMessage: async (key) => sentStore.get(key?.id) || undefined,
    msgRetryCounterCache: makeRetryCache(),
  });
  entry.sock = sock;

  sock.ev.on("creds.update", saveCreds);

  sock.ev.on("connection.update", async (u) => {
    const { connection, lastDisconnect, qr } = u;
    if (qr) {
      entry.qr = await QRCode.toDataURL(qr, { margin: 1, width: 320 });
      entry.qrAt = new Date().toISOString();
      entry.status = "qr";
      webhook({ type: "connection", session: id, status: "qr", phone: null });
    }
    if (connection === "open") {
      entry.starting = false;
      entry.status = "connected";
      entry.qr = null;
      entry.phone = (sock.user?.id || "").split(":")[0].split("@")[0] || null;
      log.info({ session: id, phone: entry.phone }, "sesión conectada");
      webhook({ type: "connection", session: id, status: "connected", phone: entry.phone });
    }
    if (connection === "close") {
      entry.starting = false;
      const code = new Boom(lastDisconnect?.error)?.output?.statusCode;
      const loggedOut = code === DisconnectReason.loggedOut;
      entry.status = loggedOut ? "logged_out" : "disconnected";
      // Detalle completo del cierre (razón del stream error, p. ej.
      // device_removed / logged_out / conflict) para diagnosticar 401.
      const err = lastDisconnect?.error;
      const detail = {
        message: err?.message, reason: err?.data?.attrs?.reason || err?.output?.payload?.message,
        attrs: err?.data?.attrs, tag: err?.data?.tag,
        content: Array.isArray(err?.data?.content) ? err.data.content.map((c) => ({ tag: c.tag, attrs: c.attrs })) : undefined,
      };
      log.warn({ session: id, code, loggedOut, detail }, "sesión cerrada");
      try { fs.appendFileSync(path.join(SESSIONS_DIR, "disconnects.log"), JSON.stringify({ at: new Date().toISOString(), session: id, code, loggedOut, detail }) + "\n"); } catch (e) { /* opcional */ }
      webhook({ type: "connection", session: id, status: entry.status, phone: entry.phone });
      if (loggedOut) {
        try { fs.rmSync(dir, { recursive: true, force: true }); } catch {}
        entry.phone = null;
      } else {
        setTimeout(() => startSession(id).catch((e) => log.error(e, "reconexión falló")), 3000);
      }
    }
  });

  sock.ev.on("messages.upsert", async ({ messages, type }) => {
    if (type !== "notify") return;
    for (const m of messages) {
      if (!m.message || m.key.fromMe) continue;
      const jid = m.key.remoteJid || "";
      if (jid.endsWith("@g.us") || jid === "status@broadcast") continue; // grupos/estados: fuera
      if (m.key.id && seenIds.has(m.key.id)) continue; // Baileys reentrega (sync/reintentos)
      rememberId(m.key.id);
      // Chats @lid (identidad nueva de WhatsApp): el teléfono real viaja en senderPn.
      const pnJid = jid.endsWith("@lid") ? (m.key.senderPn || m.key.participantPn || "") : jid;
      const from = (pnJid || "").split("@")[0].split(":")[0];
      const msg = m.message;
      const text = msg.conversation || msg.extendedTextMessage?.text || msg.imageMessage?.caption
        || msg.videoMessage?.caption || msg.documentMessage?.caption || "";
      const mediaNode = msg.imageMessage || msg.documentMessage || msg.audioMessage || msg.videoMessage;
      const payload = {
        type: "message", session: id, id: m.key.id, jid,
        from, lid: jid.endsWith("@lid") ? jid.split("@")[0] : null,
        pushname: m.pushName || null, text,
        timestamp: Number(m.messageTimestamp) * 1000 || Date.now(),
        has_media: !!mediaNode, mimetype: mediaNode?.mimetype || null,
        filename: msg.documentMessage?.fileName || null,
      };
      if (mediaNode && DOWNLOAD_MEDIA) {
        try {
          const buf = await downloadMediaMessage(m, "buffer", {}, { logger: pino({ level: "silent" }), reuploadRequest: sock.updateMediaMessage });
          if (buf && buf.length < 15 * 1024 * 1024) payload.base64 = buf.toString("base64");
        } catch (e) { log.warn({ err: e.message }, "no se pudo descargar el medio"); }
      }
      webhook(payload);
      if (entry.options?.mark_read) { try { await sock.readMessages([m.key]); } catch (e) { /* cosmético */ } }
    }
  });

  sock.ev.on("messages.update", (updates) => {
    for (const u of updates) {
      const st = u.update?.status;
      if (st === undefined || st === null) continue;
      const map = { 0: "failed", 1: "pending", 2: "sent", 3: "delivered", 4: "read", 5: "played" };
      webhook({ type: "status", session: id, id: u.key.id, jid: u.key.remoteJid, status: map[st] || String(st) });
    }
  });

  return entry;
}

// ───────────────────────────── API ─────────────────────────────
const app = express();
app.use(express.json({ limit: "25mb" }));
app.use((req, res, next) => {
  if (req.path === "/health") return next();
  if (req.get("x-api-key") !== API_KEY) return res.status(401).json({ error: "API key inválida" });
  next();
});
const wrap = (fn) => (req, res) => fn(req, res).catch((e) => { log.error(e); res.status(400).json({ error: e.message || String(e) }); });

app.get("/health", (req, res) => res.json({ ok: true, sessions: [...sessions.keys()], uptime: process.uptime() }));
app.get("/sessions", (req, res) => res.json({ sessions: [...sessions.keys()].map(sessionState) }));
app.post("/sessions/:id/start", wrap(async (req, res) => { await startSession(req.params.id, req.body || {}); res.json(sessionState(req.params.id)); }));
app.get("/sessions/:id/status", (req, res) => res.json(sessionState(req.params.id)));
app.delete("/sessions/:id", wrap(async (req, res) => {
  const s = sessions.get(req.params.id);
  if (s?.sock) { try { await s.sock.logout(); } catch {} try { s.sock.end(undefined); } catch {} }
  sessions.delete(req.params.id);
  try { fs.rmSync(path.join(SESSIONS_DIR, req.params.id), { recursive: true, force: true }); } catch {}
  res.json({ ok: true });
}));

function requireConnected(id) {
  const s = sessions.get(id);
  if (!s || s.status !== "connected" || !s.sock) throw new Error(`Sesión ${id} no conectada (${s?.status || "stopped"})`);
  return s.sock;
}

app.post("/sessions/:id/check", wrap(async (req, res) => {
  const sock = requireConnected(req.params.id);
  const phone = normalizePhone(req.body?.phone);
  const r = await sock.onWhatsApp(phone);
  const hit = Array.isArray(r) ? r.find((x) => x.exists) : null;
  res.json({ phone, exists: !!hit, jid: hit?.jid || null });
}));

app.post("/sessions/:id/send", wrap(async (req, res) => {
  const sock = requireConnected(req.params.id);
  const { to, text } = req.body || {};
  if (!text) throw new Error("text requerido");
  const jid = await resolveJid(sock, to);
  if (req.body.typing !== false) await humanTyping(sock, jid, String(text));
  const sent = await sock.sendMessage(jid, { text: String(text) });
  sessions.get(req.params.id)?.sentStore?.put(sent?.key?.id, sent?.message);
  res.json({ id: sent?.key?.id, jid });
}));

app.post("/sessions/:id/send-media", wrap(async (req, res) => {
  const sock = requireConnected(req.params.id);
  const { to, caption, mimetype, filename, base64 } = req.body || {};
  if (!base64) throw new Error("base64 requerido");
  const jid = await resolveJid(sock, to);
  if (req.body.typing !== false) await humanTyping(sock, jid, caption || "");
  const buffer = Buffer.from(base64, "base64");
  const mt = mimetype || "application/octet-stream";
  let content;
  if (mt.startsWith("image/")) content = { image: buffer, caption: caption || "" };
  else if (mt.startsWith("video/")) content = { video: buffer, caption: caption || "" };
  else if (mt.startsWith("audio/")) content = { audio: buffer, mimetype: mt };
  else content = { document: buffer, mimetype: mt, fileName: filename || "archivo", caption: caption || "" };
  const sent = await sock.sendMessage(jid, content);
  sessions.get(req.params.id)?.sentStore?.put(sent?.key?.id, sent?.message);
  res.json({ id: sent?.key?.id, jid });
}));

app.listen(PORT, () => log.info(`SOM WhatsApp Gateway escuchando en :${PORT}`));

// Reanudar sesiones con credenciales guardadas al arrancar.
for (const id of fs.readdirSync(SESSIONS_DIR, { withFileTypes: true }).filter((d) => d.isDirectory()).map((d) => d.name)) {
  startSession(id).catch((e) => log.error(e, `no se pudo reanudar ${id}`));
}
