# -*- coding: utf-8 -*-
"""Asistente IA por WhatsApp (DeepSeek, API compatible con OpenAI).

Flujo por mensaje entrante:
  1. FILTRO DETERMINISTA: el número debe estar en la lista blanca
     (whatsapp.ai.number, activo). Si no está, la IA NO se entera: el
     mensaje sigue el ruteo normal (reenvío al asesor / auto-respuesta).
  2. Audio → texto (servicio de transcripción compatible con OpenAI:
     Whisper / Groq). Sin transcriptor configurado, se avisa al remitente.
  3. Conversación con DeepSeek con HERRAMIENTAS de solo lectura sobre Odoo
     (productos, existencias, comprometido, tránsito, precios, costo ALL-IN,
     pedidos, clientes, vendedores). Cada herramienta corre como el usuario
     de Odoo ligado al número: reglas de registro y grupos aplican tal cual.
  4. La respuesta (texto) sale por la misma cuenta que recibió el mensaje.
     Todo queda en bitácora (whatsapp.ai.turn) para auditoría.

El procesamiento es asíncrono: el webhook solo marca el mensaje como
pendiente y dispara el cron; así el gateway no espera a la IA.
"""
import base64
import json
import logging
import time
from datetime import timedelta

import requests

from odoo import api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

MAX_REPLY_CHARS = 3500


def _fmt_money(amount, currency=''):
    try:
        return ('%s %s' % ('{:,.2f}'.format(float(amount or 0.0)), currency or '')).strip()
    except Exception:  # noqa: BLE001
        return str(amount)


class WhatsappAiNumber(models.Model):
    _name = 'whatsapp.ai.number'
    _description = 'Número autorizado para el asistente IA'
    _order = 'name'

    name = fields.Char(string='Nombre', required=True)
    phone = fields.Char(string='Teléfono', required=True, index=True,
                        help='Se normaliza al guardar (solo dígitos, con lada de país).')
    partner_id = fields.Many2one('res.partner', string='Contacto')
    user_id = fields.Many2one(
        'res.users', string='Usuario de Odoo (permisos)', required=True,
        help='La IA consulta Odoo CON LOS PERMISOS DE ESTE USUARIO: solo ve lo que él ve '
             '(compañías, reglas de registro, costos si tiene el grupo).')
    active = fields.Boolean(default=True)
    daily_limit = fields.Integer(string='Máximo de mensajes por día', default=200)
    reply_mode = fields.Selection([('text', 'Texto'), ('voice', 'Audio (voz ElevenLabs)')],
                                  string='Responde con', default='text', required=True)
    notes = fields.Text(string='Notas')
    extra_prompt = fields.Text(string='Instrucciones adicionales',
                               help='Se agregan al prompt del sistema solo para este número.')
    last_activity = fields.Datetime(string='Última actividad', readonly=True)
    message_count = fields.Integer(string='Mensajes atendidos', readonly=True)
    conversation_ids = fields.One2many('whatsapp.ai.conversation', 'number_id', string='Conversaciones')

    _phone_uniq = models.Constraint('unique(phone)', 'Ese teléfono ya está autorizado.')

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('phone'):
                vals['phone'] = self.env['whatsapp.gateway'].normalize_phone(vals['phone'])
        return super().create(vals_list)

    def write(self, vals):
        if vals.get('phone'):
            vals['phone'] = self.env['whatsapp.gateway'].normalize_phone(vals['phone'])
        return super().write(vals)

    @api.model
    def _find_for_phone(self, phone):
        """Filtro determinista: número autorizado y activo, o vacío."""
        phone = self.env['whatsapp.gateway'].normalize_phone(phone or '')
        if not phone:
            return self.browse()
        rec = self.sudo().search([('phone', '=', phone)], limit=1)
        if not rec and len(phone) >= 10:
            rec = self.sudo().search([('phone', 'like', phone[-10:])], limit=1)
        return rec


class WhatsappAiConversation(models.Model):
    _name = 'whatsapp.ai.conversation'
    _description = 'Conversación con el asistente IA'
    _order = 'last_activity desc'

    number_id = fields.Many2one('whatsapp.ai.number', string='Número', required=True, ondelete='cascade', index=True)
    account_id = fields.Many2one('whatsapp.account', string='Cuenta WhatsApp')
    company_id = fields.Many2one('res.company', string='Compañía', index=True)
    last_activity = fields.Datetime(string='Última actividad')
    turn_ids = fields.One2many('whatsapp.ai.turn', 'conversation_id', string='Turnos')
    draft_quote_json = fields.Text(string='Borrador de cotización',
                                   help='Estado del flujo guiado de cotización por audio (JSON).')
    last_quote_order_id = fields.Many2one('sale.order', string='Última cotización creada')
    turn_count = fields.Integer(compute='_compute_turn_count')
    name = fields.Char(compute='_compute_name')

    def _compute_turn_count(self):
        for rec in self:
            rec.turn_count = len(rec.turn_ids)

    def _compute_name(self):
        for rec in self:
            rec.name = '%s · %s' % (rec.number_id.name or rec.number_id.phone, rec.account_id.name or '')


class WhatsappAiTurn(models.Model):
    _name = 'whatsapp.ai.turn'
    _description = 'Turno de conversación IA'
    _order = 'id'

    conversation_id = fields.Many2one('whatsapp.ai.conversation', required=True, ondelete='cascade', index=True)
    role = fields.Selection([('user', 'Usuario'), ('assistant', 'Asistente'), ('tool', 'Herramienta'), ('system', 'Sistema')], required=True)
    content = fields.Text(string='Contenido')
    tool_name = fields.Char(string='Herramienta')
    tool_args = fields.Text(string='Argumentos')
    tool_call_id = fields.Char()
    message_id = fields.Many2one('whatsapp.message', string='Mensaje entrante')
    reply_message_id = fields.Many2one('whatsapp.message', string='Respuesta enviada')
    was_audio = fields.Boolean(string='Venía en audio')
    tokens_in = fields.Integer(string='Tokens entrada')
    tokens_out = fields.Integer(string='Tokens salida')
    latency_ms = fields.Integer(string='Latencia (ms)')
    error = fields.Text()


class WhatsappMessageAi(models.Model):
    _inherit = 'whatsapp.message'

    ai_state = fields.Selection([
        ('none', 'No aplica'), ('pending', 'Pendiente IA'), ('processing', 'Procesando'),
        ('done', 'Respondido por IA'), ('error', 'Error IA'), ('skipped', 'Omitido'),
    ], string='IA', default='none', index=True, readonly=True)
    ai_number_id = fields.Many2one('whatsapp.ai.number', string='Número IA', readonly=True)


class WhatsappAiAssistant(models.AbstractModel):
    _name = 'whatsapp.ai.assistant'
    _description = 'Orquestador del asistente IA'

    # ── configuración ──
    @api.model
    def _cfg(self):
        # Lectura SIN caché: get_param usa ormcache por proceso y una clave
        # cargada desde fuera (shell, otro worker) tardaba en verse — el
        # síntoma real fue "transcripción no configurada" con la clave ya
        # puesta. La config de la IA se lee una vez por mensaje: SQL directo.
        self.env.cr.execute(
            "SELECT key, value FROM ir_config_parameter WHERE key LIKE 'som_whatsapp.ai_%'")
        raw = dict(self.env.cr.fetchall())

        class P:  # misma interfaz que ir.config_parameter para el resto del método
            @staticmethod
            def get_param(key, default=False):
                v = raw.get(key)
                return v if v not in (None, '') else default
        def _int(key, default):
            try:
                return int(P.get_param(key, default) or default)
            except (TypeError, ValueError):
                return default
        def _float(key, default):
            try:
                return float(P.get_param(key, default) or default)
            except (TypeError, ValueError):
                return default
        return {
            'enabled': P.get_param('som_whatsapp.ai_enabled', 'False') in ('True', 'true', '1'),
            'api_url': (P.get_param('som_whatsapp.ai_api_url') or 'https://api.deepseek.com').rstrip('/'),
            'api_key': P.get_param('som_whatsapp.ai_api_key') or '',
            'model': P.get_param('som_whatsapp.ai_model') or 'deepseek-chat',
            'temperature': _float('som_whatsapp.ai_temperature', 0.2),
            'max_tool_calls': _int('som_whatsapp.ai_max_tool_calls', 6),
            'context_turns': _int('som_whatsapp.ai_context_turns', 12),
            'timeout': _int('som_whatsapp.ai_timeout', 60),
            'system_prompt': P.get_param('som_whatsapp.ai_system_prompt') or '',
            'stt_url': (P.get_param('som_whatsapp.ai_stt_url') or '').rstrip('/'),
            'stt_key': P.get_param('som_whatsapp.ai_stt_key') or '',
            'stt_model': P.get_param('som_whatsapp.ai_stt_model') or 'whisper-1',
            'tts_url': (P.get_param('som_whatsapp.ai_tts_url') or 'https://api.elevenlabs.io').rstrip('/'),
            'tts_key': P.get_param('som_whatsapp.ai_tts_key') or '',
            'tts_voice': P.get_param('som_whatsapp.ai_tts_voice') or '',
            'tts_model': P.get_param('som_whatsapp.ai_tts_model') or 'eleven_v3',
            'tts_format': P.get_param('som_whatsapp.ai_tts_format') or 'opus_48000_64',
            'tts_max_chars': _int('som_whatsapp.ai_tts_max_chars', 2000),
        }

    # ── 1. filtro determinista (lo llama el webhook) ──
    @api.model
    def _gate(self, message):
        """True si ESTE mensaje lo atiende la IA. Nunca decide la IA: decide la
        lista blanca. Deja el mensaje en 'pending' y dispara el cron."""
        cfg = self._cfg()
        if not cfg['enabled'] or not message or message.direction != 'in':
            return False
        number = self.env['whatsapp.ai.number']._find_for_phone(message.phone)
        if not number or not number.user_id or not number.user_id.active:
            return False
        if not (message.body or '').strip() and not message.attachment_id:
            return False
        if message.ai_state in ('pending', 'processing', 'done'):
            return True  # ya está en la cola o atendido: no re-marcar
        message.sudo().write({'ai_state': 'pending', 'ai_number_id': number.id})
        cron = self.env.ref('som_whatsapp.ir_cron_whatsapp_ai_process', raise_if_not_found=False)
        if cron:
            try:
                cron.sudo()._trigger()
            except Exception:  # noqa: BLE001
                _logger.exception('[WHATSAPP IA] no se pudo disparar el cron')
        return True

    @api.model
    def _cron_process(self, limit=20):
        """ANTI-DUPLICADOS. El envío a WhatsApp es un efecto EXTERNO que un
        rollback no deshace, así que la regla es: (1) cada mensaje se RECLAMA
        ('processing' + commit, con FOR UPDATE SKIP LOCKED para que dos
        procesos no tomen el mismo pendiente); (2) un mensaje interrumpido a
        media respuesta JAMÁS se reintenta — se marca error y el usuario
        pregunta de nuevo — porque reintentar es lo que duplica respuestas."""
        Msg = self.env['whatsapp.message'].sudo()
        # Rescate de huérfanos: 'processing' viejo = corrida interrumpida
        # (reinicio, caída). Se cierra como error SIN responder.
        stale = Msg.search([('ai_state', '=', 'processing'),
                            ('write_date', '<', fields.Datetime.now() - timedelta(minutes=30))])
        if stale:
            stale.write({'ai_state': 'error',
                         'error': 'IA: procesamiento interrumpido; no se reintenta para no duplicar respuestas.'})
            self.env.cr.commit()
        self.env.cr.execute(
            """SELECT id FROM whatsapp_message
               WHERE ai_state = 'pending' AND direction = 'in'
               ORDER BY id LIMIT %s FOR UPDATE SKIP LOCKED""", (limit,))
        ids = [r[0] for r in self.env.cr.fetchall()]
        for mid in ids:
            msg = Msg.browse(mid)
            # Reclamo comprometido: desde aquí nadie más lo puede tomar,
            # ni siquiera si lo que sigue truena y se revierte.
            msg.write({'ai_state': 'processing'})
            self.env.cr.commit()
            try:
                self._process_message(msg)
            except Exception as e:  # noqa: BLE001
                _logger.exception('[WHATSAPP IA] mensaje %s falló', msg.id)
                msg.write({'ai_state': 'error', 'error': ('IA: %s' % e)[:2000]})
            self.env.cr.commit()
        return True

    # ── 2-4. procesamiento de un mensaje ──
    @api.model
    def _process_message(self, msg):
        if msg.ai_state not in ('pending', 'processing'):
            return  # solo se procesa lo reclamado; done/error/skipped jamás se re-responden
        cfg = self._cfg()
        number = msg.ai_number_id or self.env['whatsapp.ai.number']._find_for_phone(msg.phone)
        if not number:
            msg.write({'ai_state': 'skipped'})
            return
        # tope diario por número (protege el costo y el teléfono)
        since = fields.Datetime.now() - timedelta(days=1)
        used = self.env['whatsapp.ai.turn'].sudo().search_count([
            ('conversation_id.number_id', '=', number.id), ('role', '=', 'user'), ('create_date', '>=', since)])
        if number.daily_limit and used >= number.daily_limit:
            self._reply(msg, number, 'Llegaste al máximo de consultas del día para este número. Mañana seguimos.')
            msg.write({'ai_state': 'skipped'})
            return
        conv = self._conversation_for(msg, number)
        text = (msg.body or '').strip()
        was_audio = False
        att = msg.attachment_id
        if att and (att.mimetype or '').startswith('audio/'):
            was_audio = True
            try:
                transcript = self._transcribe(att, cfg)
            except UserError as e:
                self._reply(msg, number, str(e))
                msg.write({'ai_state': 'skipped'})
                return
            text = (text + '\n' if text else '') + (transcript or '')
            if not (transcript or '').strip():
                self._reply(msg, number, 'No pude entender el audio. ¿Me lo repites o lo escribes?')
                msg.write({'ai_state': 'skipped'})
                return
        elif att and not text:
            text = '(El usuario envió un archivo %s sin texto.)' % (att.mimetype or '')
        if not text:
            msg.write({'ai_state': 'skipped'})
            return
        Turn = self.env['whatsapp.ai.turn'].sudo()
        Turn.create({'conversation_id': conv.id, 'role': 'user', 'content': text, 'message_id': msg.id, 'was_audio': was_audio})
        if not cfg['api_key']:
            self._reply(msg, number, 'El asistente aún no tiene configurada la conexión con la IA (API key). Avísale al administrador.')
            msg.write({'ai_state': 'error', 'error': 'IA sin API key'})
            return
        answer, usage, err = self._run_agent(conv, number, cfg)
        if err and not answer:
            answer = 'No pude procesar tu consulta en este momento. Intenta de nuevo en unos minutos.'
        reply = self._reply(msg, number, answer)
        # El envío YA salió: el estado se fija primero y la contabilidad va
        # en tolerante — un fallo aquí no debe re-encolar (duplicaría).
        msg.write({'ai_state': 'error' if err else 'done', 'error': err or False})
        try:
            Turn.create({'conversation_id': conv.id, 'role': 'assistant', 'content': answer, 'message_id': msg.id,
                         'reply_message_id': reply.id if reply else False, 'tokens_in': usage.get('prompt_tokens', 0),
                         'tokens_out': usage.get('completion_tokens', 0), 'latency_ms': usage.get('latency_ms', 0), 'error': err or False})
            number.sudo().write({'last_activity': fields.Datetime.now(), 'message_count': number.message_count + 1})
            conv.write({'last_activity': fields.Datetime.now()})
        except Exception:  # noqa: BLE001
            _logger.exception('[WHATSAPP IA] contabilidad del turno falló (la respuesta ya salió)')

    @api.model
    def _conversation_for(self, msg, number):
        Conv = self.env['whatsapp.ai.conversation'].sudo()
        conv = Conv.search([('number_id', '=', number.id), ('account_id', '=', msg.account_id.id)], order='id desc', limit=1)
        if not conv:
            conv = Conv.create({'number_id': number.id, 'account_id': msg.account_id.id,
                                'company_id': msg.company_id.id or number.user_id.company_id.id})
        return conv

    @api.model
    def _reply(self, msg, number, text):
        """Responde por la misma cuenta que recibió. Modo texto: en partes.
        Modo voz (ElevenLabs): un audio; si el TTS falla o el texto es muy
        largo para voz, cae a texto para nunca dejar sin respuesta."""
        text = (text or '').strip()
        if not text:
            return None
        Msg = self.env['whatsapp.message'].sudo().with_context(wa_skip_blocklist=True)
        cfg = self._cfg()
        # Bloques [TEXTO]...[/TEXTO]: listas precisas (precios, resúmenes de
        # cotización) SIEMPRE van como mensaje de texto, aunque la
        # conversación sea por audio; el resto se locuta.
        import re as _re
        text_blocks = [b.strip() for b in _re.findall(r'\[TEXTO\](.*?)\[/TEXTO\]', text, flags=_re.S) if b.strip()]
        if text_blocks:
            spoken = _re.sub(r'\[TEXTO\].*?\[/TEXTO\]', ' ', text, flags=_re.S)
            spoken = _re.sub(r'\s{2,}', ' ', spoken).strip()
            first = None
            for block in text_blocks:
                sent = Msg.queue(phone=msg.phone, body=block, partner=number.partner_id or msg.partner_id or None,
                                 account=msg.account_id or None, res_model='whatsapp.message', res_id=msg.id, send_now=True)
                first = first or sent
            if spoken:
                voiced = self._reply(msg, number, spoken)
                first = first or voiced
            return first
        if number.reply_mode == 'voice' and cfg['tts_key'] and cfg['tts_voice']:
            # Red de seguridad: montos/cantidades a palabras aunque el modelo
            # los haya dejado en cifras; el tope de caracteres se mide sobre
            # lo que realmente se locuta.
            speech = self._voice_normalize(text)
            if len(speech) <= cfg['tts_max_chars']:
                try:
                    audio_b64, mimetype = self._tts(speech, cfg)
                    ext = 'ogg' if 'ogg' in mimetype or 'opus' in cfg['tts_format'] else 'mp3'
                    return Msg.queue(phone=msg.phone, body='', partner=number.partner_id or msg.partner_id or None,
                                     account=msg.account_id or None, res_model='whatsapp.message', res_id=msg.id,
                                     attachment=('respuesta.%s' % ext, audio_b64, mimetype), send_now=True)
                except Exception as e:  # noqa: BLE001
                    _logger.warning('[WHATSAPP IA] TTS falló, se responde en texto: %s', e)
        chunks = []
        while text:
            if len(text) <= MAX_REPLY_CHARS:
                chunks.append(text); break
            cut = text.rfind('\n', 0, MAX_REPLY_CHARS)
            if cut < MAX_REPLY_CHARS // 2:
                cut = MAX_REPLY_CHARS
            chunks.append(text[:cut]); text = text[cut:].lstrip()
        first = None
        for chunk in chunks:
            try:
                sent = Msg.queue(phone=msg.phone, body=chunk, partner=number.partner_id or msg.partner_id or None,
                                 account=msg.account_id or None, res_model='whatsapp.message', res_id=msg.id, send_now=True)
                first = first or sent
            except Exception as e:  # noqa: BLE001
                _logger.exception('[WHATSAPP IA] respuesta no enviada')
                raise UserError(_('No se pudo enviar la respuesta: %s') % e)
        return first

    # ── transcripción de audio (API compatible con OpenAI /audio/transcriptions) ──
    @api.model
    def _transcribe(self, attachment, cfg=None):
        cfg = cfg or self._cfg()
        if not cfg['stt_url'] or not cfg['stt_key']:
            raise UserError(_('Recibí tu audio, pero la transcripción de voz aún no está configurada. Escríbeme tu consulta por texto.'))
        raw = base64.b64decode(attachment.datas or b'')
        if not raw:
            return ''
        mimetype = attachment.mimetype or 'audio/ogg'
        ext = {'audio/ogg': 'ogg', 'audio/ogg; codecs=opus': 'ogg', 'audio/mpeg': 'mp3', 'audio/mp4': 'm4a',
               'audio/aac': 'aac', 'audio/wav': 'wav', 'audio/webm': 'webm'}.get(mimetype.split(';')[0].strip(), 'ogg')
        files = {'file': ('audio.%s' % ext, raw, mimetype.split(';')[0].strip())}
        data = {'model': cfg['stt_model'], 'language': 'es', 'response_format': 'json'}
        t0 = time.time()
        resp = requests.post(cfg['stt_url'] + '/audio/transcriptions', headers={'Authorization': 'Bearer %s' % cfg['stt_key']},
                             files=files, data=data, timeout=cfg['timeout'])
        if resp.status_code >= 400:
            _logger.warning('[WHATSAPP IA] STT %s: %s', resp.status_code, resp.text[:300])
            raise UserError(_('No pude transcribir el audio (servicio de voz no disponible). Escríbeme tu consulta por texto.'))
        text = (resp.json() or {}).get('text') or ''
        _logger.info('[WHATSAPP IA] audio transcrito en %.1fs: %s', time.time() - t0, text[:120])
        return text.strip()


    # ── voz: montos y cantidades EN PALABRAS (los TTS leen mal "193,436.53") ──
    @staticmethod
    def _es_int_words(n):
        n = int(n)
        if n < 0:
            return 'menos ' + WhatsappAiAssistant._es_int_words(-n)
        U = ['cero', 'uno', 'dos', 'tres', 'cuatro', 'cinco', 'seis', 'siete', 'ocho', 'nueve', 'diez',
             'once', 'doce', 'trece', 'catorce', 'quince', 'dieciséis', 'diecisiete', 'dieciocho', 'diecinueve',
             'veinte', 'veintiuno', 'veintidós', 'veintitrés', 'veinticuatro', 'veinticinco', 'veintiséis',
             'veintisiete', 'veintiocho', 'veintinueve']
        T = {3: 'treinta', 4: 'cuarenta', 5: 'cincuenta', 6: 'sesenta', 7: 'setenta', 8: 'ochenta', 9: 'noventa'}
        H = {1: 'ciento', 2: 'doscientos', 3: 'trescientos', 4: 'cuatrocientos', 5: 'quinientos',
             6: 'seiscientos', 7: 'setecientos', 8: 'ochocientos', 9: 'novecientos'}
        def under1000(x):
            if x < 30:
                return U[x]
            if x < 100:
                t, u = divmod(x, 10)
                return T[t] + (' y ' + U[u] if u else '')
            if x == 100:
                return 'cien'
            c, r = divmod(x, 100)
            return H[c] + (' ' + under1000(r) if r else '')
        def apocope(w):
            return (w[:-len('uno')] + 'ún') if w.endswith('veintiuno') else ('un' if w == 'uno' else (w[:-4] + ' y un' if w.endswith('y uno') else w))
        parts = []
        millones, resto = divmod(n, 1000000)
        miles, unidades = divmod(resto, 1000)
        if millones:
            parts.append('un millón' if millones == 1 else apocope(WhatsappAiAssistant._es_int_words(millones)) + ' millones')
        if miles:
            parts.append('mil' if miles == 1 else apocope(under1000(miles)) + ' mil')
        if unidades or not parts:
            parts.append(under1000(unidades))
        return ' '.join(parts)

    @classmethod
    def _es_amount_words(cls, num_txt, unit):
        """'193,436.53' + unidad → palabras. Moneda: centavos; cantidades: punto."""
        clean = num_txt.replace(',', '').replace(' ', '')
        try:
            entero, _, dec = clean.partition('.')
            n = int(entero or 0)
            dec = (dec or '')[:2]
        except ValueError:
            return num_txt + ' ' + unit
        words = cls._es_int_words(n)
        singular = {'dólares': 'dólar', 'pesos': 'peso', 'metros cuadrados': 'metro cuadrado',
                    'empaques': 'empaque', 'cajas': 'caja', 'piezas': 'pieza', 'placas': 'placa'}
        uword = singular.get(unit, unit) if n == 1 and not dec else unit
        if unit in ('dólares', 'pesos'):
            # NUNCA se leen centavos: el monto se redondea HACIA ARRIBA al
            # entero (regla del cliente para audio).
            if dec and int(dec.ljust(2, '0')):
                n += 1
                dec = ''
                words = cls._es_int_words(n)
            uword = singular.get(unit, unit) if n == 1 else unit
            if n == 1:
                words = 'un'
            return '%s %s' % (words, uword)
        if n == 1 and not dec:
            words = 'una' if uword in ('placa', 'caja', 'pieza') else 'un'
        if unit == 'por ciento':
            return words + ((' punto ' + cls._es_int_words(int(dec))) if dec and int(dec) else '') + ' por ciento'
        out = words + ((' punto ' + cls._es_int_words(int(dec))) if dec and int(dec) else '')
        return out + (' ' + uword if uword else '')

    @classmethod
    def _voice_normalize(cls, text):
        """Prepara el texto para el TTS: montos y cantidades en palabras
        (folios, fechas y claves NO se tocan), sin asteriscos ni símbolos."""
        import re as _re
        t = text or ''
        t = t.replace('*', '').replace('_', ' ')
        t = _re.sub(r'[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F]', '', t)  # emojis
        cur_map = {'USD': 'dólares', 'MXN': 'pesos', 'dólares': 'dólares', 'dolares': 'dólares', 'pesos': 'pesos'}
        def money(m):
            return m.group(1) + cls._es_amount_words(m.group(2), cur_map.get(m.group(3), m.group(3)))
        t = _re.sub(r'(^|[\s(«"\'])\$?\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*(USD|MXN|dólares|dolares|pesos)\b', money, t)
        unit_map = {'m²': 'metros cuadrados', 'm2': 'metros cuadrados', 'pzas': 'piezas', 'pza': 'pieza',
                    'piezas': 'piezas', 'empaques': 'empaques', 'empaque': 'empaques', 'cajas': 'cajas',
                    'caja': 'cajas', 'placas': 'placas', '%': 'por ciento'}
        def qty(m):
            return cls._es_amount_words(m.group(1), unit_map.get(m.group(2), m.group(2)))
        t = _re.sub(r'\b([0-9][0-9,]*(?:\.[0-9]+)?)\s*(m²|m2|pzas?|piezas|empaques?|cajas?|placas)\b', qty, t)
        t = _re.sub(r'\b([0-9][0-9,]*(?:\.[0-9]+)?)\s*%', lambda m: cls._es_amount_words(m.group(1), 'por ciento'), t)
        return _re.sub(r'[ \t]{2,}', ' ', t).strip()

    # ── texto a voz (ElevenLabs v3 conversacional) ──
    @api.model
    def _tts(self, text, cfg=None):
        """Genera el audio con ElevenLabs. Devuelve (base64, mimetype).
        output_format opus_48000_64 llega a WhatsApp como nota de voz;
        mp3_44100_64 como archivo de audio."""
        cfg = cfg or self._cfg()
        fmt = cfg['tts_format']
        url = '%s/v1/text-to-speech/%s?output_format=%s' % (cfg['tts_url'], cfg['tts_voice'], fmt)
        payload = {'text': text, 'model_id': cfg['tts_model']}
        t0 = time.time()
        resp = requests.post(url, json=payload, headers={'xi-api-key': cfg['tts_key'], 'Content-Type': 'application/json'},
                             timeout=cfg['timeout'])
        if resp.status_code >= 400:
            raise UserError('TTS %s: %s' % (resp.status_code, resp.text[:200]))
        mimetype = 'audio/ogg; codecs=opus' if fmt.startswith('opus') else ('audio/mpeg' if fmt.startswith('mp3') else 'audio/wav')
        _logger.info('[WHATSAPP IA] TTS %s chars en %.1fs (%s)', len(text), time.time() - t0, fmt)
        return base64.b64encode(resp.content).decode(), mimetype

    # ── agente: DeepSeek + herramientas ──
    @api.model
    def _system_prompt(self, number, cfg):
        user = number.user_id
        company = user.company_id
        today = fields.Date.context_today(self.with_user(user))
        base = (
            'Eres el asistente interno de %(company)s (piedra natural: placas, formatos y piezas medidas en m²). '
            'Respondes por WhatsApp a %(name)s, que es personal de la empresa. Hoy es %(today)s.\n'
            'REGLAS:\n'
            '- Responde SIEMPRE con datos obtenidos de las herramientas; si no consultaste, dilo. No inventes folios, cantidades ni precios.\n'
            '- Sé breve y directo, en español, formato WhatsApp (*negritas* para lo importante, listas cortas). Sin markdown de encabezados ni tablas.\n'
            '- Cantidades en m² con 2 decimales; dinero con separador de miles y la moneda; fechas como "13 ago 2026".\n'
            '- "Disponible" = existencia libre; "comprometido" = reservado en ventas/entregas; "apartado" = holds activos; '
            '"en tránsito" = en camino (embarques); "taller" = en proceso.\n'
            '- Los precios P1..P5 son la escalera comercial (P1 el más alto). El costo ALL-IN solo si la herramienta lo devuelve.\n'
            '- Si la consulta es ambigua (varios productos o clientes), muestra las opciones y pide precisar.\n'
            '- Solo consultas: no puedes crear, modificar ni cancelar nada.\n'
        ) % {'company': company.name, 'name': number.name, 'today': today.strftime('%d %b %Y')}
        if number.reply_mode == 'voice':
            base += (
                '- Tu respuesta se CONVIERTE A AUDIO: escribe montos, cantidades y porcentajes EN PALABRAS '
                '("ciento noventa y tres mil cuatrocientos treinta y seis dólares con cincuenta y tres centavos", '
                '"ciento veintinueve punto nueve seis metros cuadrados"). Nada de símbolos, tablas, listas largas '
                'ni asteriscos; frases cortas y naturales, como si lo dijeras hablando. Los folios se deletrean '
                'natural ("uve trescientos noventa" para V/390).\n'
                '- MONTOS EN AUDIO: jamás digas centavos; redondea SIEMPRE hacia arriba al entero '
                '("193,436.53" se dice "ciento noventa y tres mil cuatrocientos treinta y siete dólares").\n'
                '- DISCRECIÓN DE PRECIOS EN AUDIO: puede haber un cliente escuchando cerca. Cuando pregunten '
                'el precio, di ÚNICAMENTE el precio máximo (P1) y no menciones que existen otros niveles. '
                'Los niveles P2 a P5, descuentos o costos SOLO si este mensaje los pide explícitamente '
                '(p. ej. "dame el P3", "todos los niveles", "el mínimo", "el costo"); en ese caso dilos '
                'breve y sin comentarios extra.\n'
            )
        base += (
            'COTIZACIÓN GUIADA (por audio o texto): puedes armar y crear cotizaciones con las herramientas '
            'cotizacion_borrador y crear_cotizacion. Guion del vaivén:\n'
            '1) Al pedir una cotización, identifica el CLIENTE (buscar_clientes) y cada MATERIAL '
            '(buscar_productos). Si algo es ambiguo, ofrece las opciones y pregunta cuál es.\n'
            '2) Ve armando el borrador con cotizacion_borrador (iniciar, agregar, cantidad, precio, quitar, moneda) '
            'y CONFIRMA en voz lo que entendiste tras cada paso.\n'
            '3) Cuando estén los materiales, manda los PRECIOS DE LISTA en un bloque [TEXTO]...[/TEXTO] '
            '(un renglón por material, bien separado) y pregunta si deja esos precios o te dicta otros.\n'
            '4) Con precios resueltos, repite el resumen completo (cliente, materiales, cantidades, precios) '
            'y pide la confirmación FINAL. SOLO con un sí explícito llama crear_cotizacion.\n'
            '5) Da el folio creado y pregunta si quiere que se la envíes al cliente por WhatsApp; solo con un '
            'sí explícito llama enviar_cotizacion_cliente. El resumen final también va en [TEXTO].\n'
            'Nunca inventes ids ni precios: todo sale de las herramientas o de lo que el usuario dicte.\n'
        )
        if cfg.get('system_prompt'):
            base += '\n' + cfg['system_prompt'].strip() + '\n'
        if number.extra_prompt:
            base += '\n' + number.extra_prompt.strip() + '\n'
        return base

    @api.model
    def _history(self, conv, cfg):
        turns = self.env['whatsapp.ai.turn'].sudo().search(
            [('conversation_id', '=', conv.id), ('role', 'in', ('user', 'assistant'))], order='id desc', limit=cfg['context_turns'])
        msgs = []
        for t in reversed(turns):
            msgs.append({'role': t.role, 'content': (t.content or '')[:4000]})
        return msgs

    @api.model
    def _chat(self, cfg, messages, tools):
        payload = {'model': cfg['model'], 'messages': messages, 'temperature': cfg['temperature']}
        if tools:
            payload['tools'] = tools
            payload['tool_choice'] = 'auto'
        resp = requests.post(cfg['api_url'] + '/chat/completions', json=payload,
                             headers={'Authorization': 'Bearer %s' % cfg['api_key'], 'Content-Type': 'application/json'},
                             timeout=cfg['timeout'])
        if resp.status_code >= 400:
            raise UserError('IA %s: %s' % (resp.status_code, resp.text[:300]))
        return resp.json()

    @api.model
    def _run_agent(self, conv, number, cfg):
        Tools = self.env['whatsapp.ai.tools']
        tools = Tools._definitions()
        messages = [{'role': 'system', 'content': self._system_prompt(number, cfg)}] + self._history(conv, cfg)
        usage = {'prompt_tokens': 0, 'completion_tokens': 0, 'latency_ms': 0}
        Turn = self.env['whatsapp.ai.turn'].sudo()
        t0 = time.time()
        err = None
        answer = ''
        try:
            for _i in range(cfg['max_tool_calls'] + 1):
                data = self._chat(cfg, messages, tools)
                u = data.get('usage') or {}
                usage['prompt_tokens'] += int(u.get('prompt_tokens') or 0)
                usage['completion_tokens'] += int(u.get('completion_tokens') or 0)
                choice = (data.get('choices') or [{}])[0]
                message = choice.get('message') or {}
                tool_calls = message.get('tool_calls') or []
                if not tool_calls:
                    answer = (message.get('content') or '').strip()
                    break
                # el modelo pide herramientas: se ejecutan y se le devuelven
                messages.append({'role': 'assistant', 'content': message.get('content') or '', 'tool_calls': tool_calls})
                for call in tool_calls:
                    fn = (call.get('function') or {})
                    name = fn.get('name') or ''
                    try:
                        args = json.loads(fn.get('arguments') or '{}')
                    except ValueError:
                        args = {}
                    result = Tools._run(name, args, number, conversation=conv)
                    Turn.create({'conversation_id': conv.id, 'role': 'tool', 'tool_name': name,
                                 'tool_args': json.dumps(args, ensure_ascii=False)[:2000], 'tool_call_id': call.get('id'),
                                 'content': result[:8000]})
                    messages.append({'role': 'tool', 'tool_call_id': call.get('id'), 'content': result[:12000]})
            else:
                answer = (answer or 'Necesité demasiadas consultas para responder. ¿Puedes acotar la pregunta?')
        except Exception as e:  # noqa: BLE001
            _logger.exception('[WHATSAPP IA] agente falló')
            err = str(e)[:1000]
        usage['latency_ms'] = int((time.time() - t0) * 1000)
        return answer, usage, err


class WhatsappAiTools(models.AbstractModel):
    """Herramientas de SOLO LECTURA. Cada una corre como el usuario del número
    (with_user) y en sus compañías permitidas: la IA ve lo que él ve."""
    _name = 'whatsapp.ai.tools'
    _description = 'Herramientas Odoo del asistente IA'

    LIMIT = 8

    @api.model
    def _definitions(self):
        def tool(name, desc, props, required=None):
            return {'type': 'function', 'function': {'name': name, 'description': desc,
                    'parameters': {'type': 'object', 'properties': props, 'required': required or []}}}
        return [
            tool('buscar_productos', 'Busca productos por nombre/código/color/categoría. Devuelve id, nombre, categoría, tipo, existencia resumida y precios P1..P5 (y costo ALL-IN si el usuario puede verlo).',
                 {'texto': {'type': 'string', 'description': 'Palabras del nombre, código o color'}, 'limite': {'type': 'integer'}}, ['texto']),
            tool('existencias_producto', 'Existencias detalladas de UN producto: disponible, comprometido, apartado, tránsito, taller, por ubicación, y las placas/lotes (nombre, m², medidas, ubicación, estado).',
                 {'producto_id': {'type': 'integer'}, 'texto': {'type': 'string', 'description': 'Si no tienes el id, nombre del producto'}, 'solo_disponibles': {'type': 'boolean'}}),
            tool('info_lote', 'Detalle de una placa/lote por su nombre (p.ej. S106-09, 19271-2): producto, medidas, m², ubicación, si está apartada, en venta, en tránsito o en taller.',
                 {'nombre': {'type': 'string'}}, ['nombre']),
            tool('buscar_pedidos', 'Busca pedidos/cotizaciones de venta por folio, cliente, vendedor, estado o fechas. Devuelve folio, cliente, vendedor, fecha, total, pagado, entregado y semáforo.',
                 {'folio': {'type': 'string'}, 'cliente': {'type': 'string'}, 'vendedor': {'type': 'string'},
                  'estado': {'type': 'string', 'enum': ['cotizacion', 'confirmado', 'cancelado', 'todos']},
                  'desde': {'type': 'string', 'description': 'YYYY-MM-DD'}, 'hasta': {'type': 'string', 'description': 'YYYY-MM-DD'}, 'limite': {'type': 'integer'}}),
            tool('detalle_pedido', 'Detalle de un pedido: líneas (producto, m², precio, entregado, placas asignadas), totales, pagado, pendiente, autorización de entrega y entregas.',
                 {'folio': {'type': 'string'}}, ['folio']),
            tool('buscar_clientes', 'Busca clientes por nombre/teléfono/correo. Devuelve contacto, vendedor asignado, saldo por cobrar y pedidos abiertos.',
                 {'texto': {'type': 'string'}, 'limite': {'type': 'integer'}}, ['texto']),
            tool('ventas_por_vendedor', 'Ventas confirmadas por vendedor en un periodo (monto y número de pedidos).',
                 {'desde': {'type': 'string', 'description': 'YYYY-MM-DD'}, 'hasta': {'type': 'string', 'description': 'YYYY-MM-DD'}}),
            tool('transito_producto', 'Material en tránsito (embarques en camino): por producto o general. Devuelve viaje, ETA, m², y si ya está comprometido a un pedido.',
                 {'texto': {'type': 'string', 'description': 'Nombre del producto (vacío = resumen general)'}, 'limite': {'type': 'integer'}}),
            tool('resumen_ventas', 'Resumen rápido de ventas del periodo: pedidos confirmados, monto total, top clientes y productos.',
                 {'desde': {'type': 'string'}, 'hasta': {'type': 'string'}}),
            tool('resumen_ejecutivo', 'Foto ejecutiva del negocio para dirección: ventas del periodo, cobranza, inventario, compras/tránsito y señales de alerta. Usar cuando pregunten "cómo vamos", KPIs o estado general.',
                 {}),
            tool('cartera_vencida', 'Cuentas por cobrar: saldo total y clientes con facturas vencidas (monto y días). Para dirección/cobranza.',
                 {'limite': {'type': 'integer'}}),
            tool('caja_del_dia', 'Caja y cobranza en efectivo: entradas, salidas y saldo del periodo (panel de control interno de efectivo).',
                 {'periodo': {'type': 'string', 'enum': ['dia', 'semana', 'mes']}}),
            tool('pedidos_en_riesgo', 'Pedidos confirmados con foco de atención según el semáforo de flujo: sin pago, estancados o dejados, con días y monto pendiente.',
                 {'limite': {'type': 'integer'}}),
            tool('cotizacion_borrador', 'Arma la cotización EN CURSO de esta conversación, paso a paso. Acciones: iniciar (cliente_id), agregar (producto_id, cantidad m²), cantidad/precio/quitar (linea 1-based), moneda (USD|MXN), ver, cancelar. Devuelve siempre el borrador completo con precios de lista P1.',
                 {'accion': {'type': 'string', 'enum': ['iniciar', 'agregar', 'cantidad', 'precio', 'quitar', 'moneda', 'ver', 'cancelar']},
                  'cliente_id': {'type': 'integer'}, 'producto_id': {'type': 'integer'}, 'linea': {'type': 'integer', 'description': 'número de renglón (1, 2, 3…)'},
                  'cantidad': {'type': 'number'}, 'precio': {'type': 'number'}, 'moneda': {'type': 'string', 'enum': ['USD', 'MXN']}}, ['accion']),
            tool('crear_cotizacion', 'Crea en Odoo la cotización del borrador de esta conversación (SOLO tras la confirmación final explícita del usuario). Devuelve el folio.',
                 {}),
            tool('enviar_cotizacion_cliente', 'Envía al CLIENTE por WhatsApp el resumen de la última cotización creada en esta conversación (SOLO si el usuario lo pidió explícitamente).',
                 {}),
        ]

    # ── ejecución ──
    @api.model
    def _run(self, name, args, number, conversation=None):
        fn = getattr(self, '_t_' + name, None)
        if not fn:
            return json.dumps({'error': 'herramienta desconocida: %s' % name})
        user = number.user_id
        env = self.env(user=user.id, context=dict(self.env.context, allowed_company_ids=user.company_ids.ids or [user.company_id.id], lang='es_MX'))
        self._ctx_conversation = conversation
        self._ctx_number = number
        try:
            with self.env.cr.savepoint():
                result = fn(env, args or {})
            return json.dumps(result, ensure_ascii=False, default=str)
        except Exception as e:  # noqa: BLE001
            _logger.warning('[WHATSAPP IA] herramienta %s falló: %s', name, e)
            return json.dumps({'error': 'no pude consultar: %s' % str(e)[:200]})

    # helpers
    @staticmethod
    def _limit(args, default=8, cap=25):
        try:
            return max(1, min(int(args.get('limite') or default), cap))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _date(s):
        return s if s else False

    @staticmethod
    def _dfmt(d):
        if not d:
            return ''
        try:
            from odoo.addons.som_whatsapp.models.whatsapp_message import som_format_date
            return som_format_date(d)
        except Exception:  # noqa: BLE001
            return str(d)[:10]

    def _can_see_costs(self, env):
        return env.user.has_group('product_cost_security.group_product_cost_viewer') or env.user.has_group('base.group_system')

    def _product_prices(self, env, tmpl):
        t = tmpl.with_company(env.company)
        out = {}
        for i in range(1, 6):
            usd = getattr(t, 'x_price_usd_%d' % i, None)
            mxn = getattr(t, 'x_price_mxn_%d' % i, None)
            if usd or mxn:
                out['P%d' % i] = {'USD': round(usd or 0, 2), 'MXN': round(mxn or 0, 2)}
        if not out and t.list_price:
            out['lista'] = {env.company.currency_id.name: round(t.list_price, 2)}
        if self._can_see_costs(env):
            cost = {}
            for f, k in (('x_costo_mayor', 'all_in_mxn'), ('x_costo_mayor_usd', 'all_in_usd'), ('x_cost_base_mxn', 'base_mxn'), ('x_logistics_cost_mxn', 'logistica_mxn'), ('x_duty_cost_mxn', 'arancel_mxn'), ('standard_price', 'costo_estandar')):
                if f in t._fields and getattr(t, f):
                    cost[k] = round(getattr(t, f), 2)
            if cost:
                out['costo'] = cost
        return out

    def _stock_summary(self, env, product):
        Q = env['stock.quant']
        quants = Q.search([('product_id', '=', product.id), ('quantity', '>', 0)])
        s = {'disponible': 0.0, 'comprometido': 0.0, 'apartado': 0.0, 'transito': 0.0, 'taller': 0.0, 'placas': 0}
        Loc = env['stock.location']
        for q in quants:
            loc = q.location_id
            is_transit = loc._som_is_transit() if hasattr(loc, '_som_is_transit') else loc.usage == 'transit'
            if is_transit:
                s['transito'] += q.quantity
            elif loc.usage == 'production':
                s['taller'] += q.quantity
            elif loc.usage == 'internal':
                held = getattr(q, 'x_tiene_hold', False)
                if held:
                    s['apartado'] += q.quantity
                else:
                    free = q.quantity - q.reserved_quantity
                    s['disponible'] += max(free, 0.0)
                    s['comprometido'] += min(q.reserved_quantity, q.quantity)
                s['placas'] += 1
        return {k: (round(v, 2) if isinstance(v, float) else v) for k, v in s.items()}

    def _lot_status(self, env, quant):
        st = []
        if getattr(quant, 'x_tiene_hold', False):
            st.append('apartado' + (' para %s' % quant.x_hold_para if getattr(quant, 'x_hold_para', False) else ''))
        if quant.reserved_quantity > 0:
            st.append('comprometido')
        loc = quant.location_id
        if hasattr(loc, '_som_is_transit') and loc._som_is_transit():
            st.append('en tránsito')
        elif loc.usage == 'production':
            st.append('en taller')
        return ', '.join(st) or 'libre'

    def _lot_row(self, env, quant):
        lot = quant.lot_id
        return {
            'lote': lot.name, 'm2': round(quant.quantity, 2), 'libre_m2': round(max(quant.quantity - quant.reserved_quantity, 0), 2),
            'medidas': ('%.2f x %.2f m' % (lot.x_alto, lot.x_ancho)) if getattr(lot, 'x_alto', 0) and getattr(lot, 'x_ancho', 0) else '',
            'grosor': getattr(lot, 'x_grosor', '') or '', 'tipo': getattr(lot, 'x_tipo', '') or '',
            'bloque': getattr(lot, 'x_bloque', '') or '', 'ubicacion': quant.location_id.complete_name,
            'estado': self._lot_status(env, quant),
        }

    def _find_products(self, env, text, limit):
        """Búsqueda por palabras en nombre/código/color/CATEGORÍA. Prioriza
        piedra (productos con tipo placa/formato/pieza) y con existencia, y
        deja al final accesorios/adhesivos que solo coinciden por nombre."""
        Prod = env['product.product']
        dom = [('type', '=', 'consu'), ('sale_ok', '=', True)]
        words = [w for w in (text or '').split() if len(w) > 1]
        tmpl_fields = env['product.template']._fields
        d = list(dom)
        for w in words:
            ors = [('name', 'ilike', w), ('default_code', 'ilike', w), ('categ_id.complete_name', 'ilike', w)]
            if 'x_color' in tmpl_fields:
                ors.append(('product_tmpl_id.x_color', 'ilike', w))
            d += ['|'] * (len(ors) - 1) + ors
        prods = Prod.search(d, limit=max(limit * 6, 30))
        if not prods and text:
            prods = Prod.search(dom + [('name', 'ilike', text)], limit=limit)
        def score(p):
            cat = (p.categ_id.complete_name or '').lower()
            stone = 1 if any(k in cat for k in ('placa', 'formato', 'pieza', 'piedra')) else 0
            qty = sum(env['stock.quant'].search([('product_id', '=', p.id), ('quantity', '>', 0), ('location_id.usage', '=', 'internal')]).mapped('quantity'))
            return (-stone, -(1 if qty > 0 else 0), -qty, p.display_name)
        return Prod.browse([p.id for p in sorted(prods, key=score)[:limit]])

    # ── herramientas ──
    def _t_buscar_productos(self, env, args):
        limit = self._limit(args)
        prods = self._find_products(env, args.get('texto', ''), limit)
        out = []
        for p in prods:
            out.append({'id': p.id, 'producto': p.display_name, 'categoria': p.categ_id.display_name,
                        'tipo': getattr(p.product_tmpl_id, 'x_tipo', '') or '', 'existencia': self._stock_summary(env, p),
                        'precios': self._product_prices(env, p.product_tmpl_id)})
        return {'resultados': out, 'nota': 'sin coincidencias' if not out else ''}

    def _t_existencias_producto(self, env, args):
        Prod = env['product.product']
        prod = Prod.browse(int(args['producto_id'])).exists() if args.get('producto_id') else Prod
        if not prod:
            prods = self._find_products(env, args.get('texto', ''), 5)
            if len(prods) > 1:
                return {'ambiguo': True, 'opciones': [{'id': p.id, 'producto': p.display_name} for p in prods]}
            prod = prods[:1]
        if not prod:
            return {'error': 'producto no encontrado'}
        quants = env['stock.quant'].search([('product_id', '=', prod.id), ('quantity', '>', 0), ('lot_id', '!=', False)], order='location_id, lot_id')
        if args.get('solo_disponibles'):
            quants = quants.filtered(lambda q: q.location_id.usage == 'internal' and q.quantity - q.reserved_quantity > 0 and not getattr(q, 'x_tiene_hold', False))
        rows = [self._lot_row(env, q) for q in quants[:40]]
        por_ubic = {}
        for q in quants:
            k = q.location_id.complete_name
            por_ubic[k] = round(por_ubic.get(k, 0) + q.quantity, 2)
        return {'producto': prod.display_name, 'id': prod.id, 'resumen': self._stock_summary(env, prod),
                'por_ubicacion': por_ubic, 'lotes': rows, 'lotes_omitidos': max(len(quants) - 40, 0),
                'precios': self._product_prices(env, prod.product_tmpl_id)}

    def _t_info_lote(self, env, args):
        name = (args.get('nombre') or '').strip()
        lots = env['stock.lot'].search([('name', '=ilike', name)], limit=3) or env['stock.lot'].search([('name', 'ilike', name)], limit=5)
        if not lots:
            return {'error': 'lote no encontrado'}
        out = []
        for lot in lots:
            quants = env['stock.quant'].search([('lot_id', '=', lot.id), ('quantity', '!=', 0)])
            row = {'lote': lot.name, 'producto': lot.product_id.display_name, 'tipo': getattr(lot, 'x_tipo', '') or '',
                   'medidas': ('%.2f x %.2f m' % (lot.x_alto, lot.x_ancho)) if getattr(lot, 'x_alto', 0) and getattr(lot, 'x_ancho', 0) else '',
                   'grosor': getattr(lot, 'x_grosor', '') or '', 'bloque': getattr(lot, 'x_bloque', '') or '',
                   'contenedor': getattr(lot, 'x_contenedor', '') or '',
                   'existencias': [{'ubicacion': q.location_id.complete_name, 'm2': round(q.quantity, 2), 'estado': self._lot_status(env, q)} for q in quants]}
            sol = env['sale.order.line'].search([('lot_ids', 'in', lot.id)], limit=3) if 'lot_ids' in env['sale.order.line']._fields else env['sale.order.line']
            if sol:
                row['en_pedidos'] = [{'folio': l.order_id.name, 'cliente': l.order_id.partner_id.name, 'estado': l.order_id.state} for l in sol]
            out.append(row)
        return {'lotes': out}

    def _order_row(self, env, so):
        f = so._fields
        row = {'folio': so.name, 'cliente': so.partner_id.name, 'vendedor': so.user_id.name, 'fecha': self._dfmt(so.date_order),
               'estado': dict(f['state'].selection).get(so.state, so.state), 'total': _fmt_money(so.amount_total, so.currency_id.name)}
        if 'delivery_paid_amount' in f:
            row['pagado'] = _fmt_money(so.delivery_paid_amount, so.currency_id.name)
        if 'x_flow_status' in f and so.x_flow_status:
            row['semaforo'] = dict(f['x_flow_status'].selection).get(so.x_flow_status, so.x_flow_status)
        if 'x_fulfillment_net_pct' in f:
            row['entregado_pct'] = round(so.x_fulfillment_net_pct or 0, 1)
        if 'delivery_auth_state' in f:
            row['autorizacion_entrega'] = dict(f['delivery_auth_state'].selection).get(so.delivery_auth_state, so.delivery_auth_state)
        return row

    def _t_buscar_pedidos(self, env, args):
        limit = self._limit(args)
        dom = []
        if args.get('folio'):
            dom.append(('name', 'ilike', args['folio']))
        if args.get('cliente'):
            dom.append(('partner_id', 'ilike', args['cliente']))
        if args.get('vendedor'):
            dom.append(('user_id', 'ilike', args['vendedor']))
        st = args.get('estado') or 'todos'
        if st == 'cotizacion':
            dom.append(('state', 'in', ('draft', 'sent')))
        elif st == 'confirmado':
            dom.append(('state', '=', 'sale'))
        elif st == 'cancelado':
            dom.append(('state', '=', 'cancel'))
        if args.get('desde'):
            dom.append(('date_order', '>=', args['desde']))
        if args.get('hasta'):
            dom.append(('date_order', '<=', args['hasta'] + ' 23:59:59'))
        orders = env['sale.order'].search(dom, order='date_order desc', limit=limit)
        return {'pedidos': [self._order_row(env, so) for so in orders], 'total_encontrados': env['sale.order'].search_count(dom)}

    def _t_detalle_pedido(self, env, args):
        so = env['sale.order'].search([('name', '=ilike', (args.get('folio') or '').strip())], limit=1) or env['sale.order'].search([('name', 'ilike', (args.get('folio') or '').strip())], limit=1)
        if not so:
            return {'error': 'pedido no encontrado'}
        row = self._order_row(env, so)
        lines = []
        for l in so.order_line.filtered(lambda x: not x.display_type):
            lf = l._fields
            item = {'producto': l.product_id.display_name, 'cantidad': round(l.product_uom_qty, 2), 'uom': l.product_uom_id.name,
                    'precio_unitario': _fmt_money(l.price_unit, so.currency_id.name), 'subtotal': _fmt_money(l.price_subtotal, so.currency_id.name),
                    'entregado': round(l.qty_delivered, 2)}
            if 'lot_ids' in lf and l.lot_ids:
                item['placas'] = l.lot_ids.mapped('name')[:20]
            if 'tc_qty_pending_allocation' in lf:
                item['pendiente_asignar'] = round(l.tc_qty_pending_allocation or 0, 2)
            if 'standard_pack_id' in lf and l.standard_pack_id:
                item['empaque'] = '%s × %g' % (l.standard_pack_id.display_name, l.pack_qty or 0)
            lines.append(item)
        row['lineas'] = lines
        row['subtotal'] = _fmt_money(so.amount_untaxed, so.currency_id.name)
        row['impuestos'] = _fmt_money(so.amount_tax, so.currency_id.name)
        if 'amount_pending_to_pay' in so._fields:
            row['pendiente_pago'] = _fmt_money(so.amount_pending_to_pay, so.currency_id.name)
        if 'delivery_document_ids' in so._fields:
            row['entregas'] = [{'folio': d.name, 'tipo': d.document_type, 'estado': d.state, 'fecha': self._dfmt(d.delivery_date)} for d in so.delivery_document_ids[:10]]
        if so.invoice_ids:
            row['facturas'] = [{'folio': i.name, 'estado_pago': i.payment_state, 'total': _fmt_money(i.amount_total, i.currency_id.name)} for i in so.invoice_ids[:10]]
        return row

    def _t_buscar_clientes(self, env, args):
        limit = self._limit(args)
        text = (args.get('texto') or '').strip()
        dom = ['|', '|', ('name', 'ilike', text), ('phone', 'ilike', text[-10:] if len(text) >= 7 else text), ('email', 'ilike', text)]
        partners = env['res.partner'].search(dom, limit=limit)
        out = []
        for p in partners:
            open_orders = env['sale.order'].search([('partner_id', 'child_of', p.id), ('state', '=', 'sale')], limit=5, order='date_order desc')
            row = {'id': p.id, 'cliente': p.display_name, 'telefono': p.phone or '', 'correo': p.email or '',
                   'vendedor': p.user_id.name or '', 'ciudad': p.city or ''}
            if 'credit' in p._fields:
                row['saldo_por_cobrar'] = _fmt_money(p.credit, env.company.currency_id.name)
            row['pedidos_abiertos'] = [{'folio': so.name, 'total': _fmt_money(so.amount_total, so.currency_id.name), 'fecha': self._dfmt(so.date_order)} for so in open_orders]
            out.append(row)
        return {'clientes': out}

    def _t_ventas_por_vendedor(self, env, args):
        dom = [('state', '=', 'sale')]
        if args.get('desde'):
            dom.append(('date_order', '>=', args['desde']))
        if args.get('hasta'):
            dom.append(('date_order', '<=', args['hasta'] + ' 23:59:59'))
        groups = env['sale.order']._read_group(dom, ['user_id'], ['amount_total:sum', '__count'])
        cur = env.company.currency_id.name
        rows = sorted([{'vendedor': (u.name if u else 'Sin vendedor'), 'pedidos': c, 'monto': round(a or 0, 2)} for u, a, c in groups], key=lambda r: -r['monto'])
        return {'moneda': cur, 'vendedores': rows[:25], 'periodo': {'desde': args.get('desde'), 'hasta': args.get('hasta')}}

    def _t_transito_producto(self, env, args):
        if 'stock.transit.line' not in env:
            return {'error': 'tránsito no disponible'}
        limit = self._limit(args, default=15, cap=40)
        dom = [('voyage_id.custom_status', 'not in', ('delivered', 'cancel'))]
        text = (args.get('texto') or '').strip()
        if text:
            dom.append(('product_id', 'ilike', text))
        lines = env['stock.transit.line'].search(dom, order='eta asc', limit=limit)
        out = []
        for l in lines:
            v = l.voyage_id
            out.append({'viaje': v.name, 'estatus': dict(v._fields['custom_status'].selection).get(v.custom_status, v.custom_status),
                        'eta': self._dfmt(v.eta), 'producto': l.product_id.display_name, 'm2': round(l.product_uom_qty or 0, 2),
                        'comprometido_a': l.order_id.name if l.order_id else '', 'contenedor': l.container_number or v.container_number or ''})
        return {'en_transito': out}

    def _t_resumen_ventas(self, env, args):
        dom = [('state', '=', 'sale')]
        if args.get('desde'):
            dom.append(('date_order', '>=', args['desde']))
        if args.get('hasta'):
            dom.append(('date_order', '<=', args['hasta'] + ' 23:59:59'))
        orders = env['sale.order'].search(dom)
        cur = env.company.currency_id.name
        by_client = {}
        for so in orders:
            by_client[so.partner_id.name] = by_client.get(so.partner_id.name, 0) + so.amount_total
        top_clients = sorted(by_client.items(), key=lambda x: -x[1])[:5]
        by_prod = {}
        for l in orders.mapped('order_line').filtered(lambda x: not x.display_type and x.product_id.type == 'consu'):
            by_prod[l.product_id.display_name] = by_prod.get(l.product_id.display_name, 0) + l.product_uom_qty
        top_prods = sorted(by_prod.items(), key=lambda x: -x[1])[:5]
        return {'pedidos': len(orders), 'monto_total': _fmt_money(sum(orders.mapped('amount_total')), cur),
                'top_clientes': [{'cliente': c, 'monto': round(m, 2)} for c, m in top_clients],
                'top_productos_m2': [{'producto': p, 'm2': round(q, 2)} for p, q in top_prods]}

    # ── cotización guiada por conversación ──
    def _quote_draft(self):
        conv = getattr(self, '_ctx_conversation', None)
        if not conv:
            return None, {'error': 'sin conversación activa'}
        try:
            draft = json.loads(conv.sudo().draft_quote_json or '{}')
        except ValueError:
            draft = {}
        return conv, draft

    def _quote_save(self, conv, draft):
        conv.sudo().write({'draft_quote_json': json.dumps(draft, ensure_ascii=False)})

    def _quote_list_price(self, env, product, currency):
        t = product.product_tmpl_id.with_company(env.company)
        f = 'x_price_usd_1' if currency == 'USD' else 'x_price_mxn_1'
        price = getattr(t, f, 0.0) if f in t._fields else 0.0
        return round(price or (t.list_price if currency != 'USD' else 0.0) or 0.0, 2)

    def _quote_view(self, env, draft):
        cur = draft.get('moneda') or 'MXN'
        lineas = []
        total = 0.0
        for i, l in enumerate(draft.get('lineas') or [], 1):
            precio = l.get('precio') if l.get('precio') else l.get('precio_lista')
            importe = round((precio or 0.0) * (l.get('cantidad') or 0.0), 2)
            total += importe
            lineas.append({'n': i, 'producto': l.get('producto'), 'cantidad_m2': l.get('cantidad'),
                           'precio': precio, 'origen_precio': 'personalizado' if l.get('precio') else 'lista P1',
                           'precio_lista_P1': l.get('precio_lista'), 'importe': importe})
        return {'cliente': draft.get('partner_name'), 'cliente_id': draft.get('partner_id'),
                'moneda': cur, 'lineas': lineas, 'total_estimado': round(total, 2),
                'folio': draft.get('folio') or None,
                'nota': 'sin cliente' if not draft.get('partner_id') else ('sin materiales' if not lineas else '')}

    def _t_cotizacion_borrador(self, env, args):
        conv, draft = self._quote_draft()
        if conv is None:
            return draft
        accion = args.get('accion') or 'ver'
        draft.setdefault('lineas', [])
        draft.setdefault('moneda', 'MXN')
        if accion == 'iniciar':
            partner = env['res.partner'].browse(int(args.get('cliente_id') or 0)).exists()
            if not partner:
                return {'error': 'cliente no encontrado; usa buscar_clientes primero'}
            draft = {'partner_id': partner.id, 'partner_name': partner.display_name,
                     'moneda': draft.get('moneda', 'MXN'), 'lineas': [], 'folio': None}
        elif accion == 'agregar':
            product = env['product.product'].browse(int(args.get('producto_id') or 0)).exists()
            if not product:
                return {'error': 'producto no encontrado; usa buscar_productos primero'}
            qty = float(args.get('cantidad') or 0.0)
            if qty <= 0:
                return {'error': 'falta la cantidad en m² para %s' % product.display_name}
            draft['lineas'].append({'product_id': product.id, 'producto': product.display_name,
                                    'cantidad': qty, 'precio': None,
                                    'precio_lista': self._quote_list_price(env, product, draft['moneda'])})
        elif accion in ('cantidad', 'precio', 'quitar'):
            idx = int(args.get('linea') or 0) - 1
            if idx < 0 or idx >= len(draft['lineas']):
                return {'error': 'renglón inexistente; usa ver para revisar los números'}
            if accion == 'quitar':
                draft['lineas'].pop(idx)
            elif accion == 'cantidad':
                draft['lineas'][idx]['cantidad'] = float(args.get('cantidad') or 0.0)
            else:
                draft['lineas'][idx]['precio'] = float(args.get('precio') or 0.0) or None
        elif accion == 'moneda':
            draft['moneda'] = 'USD' if (args.get('moneda') or '').upper() == 'USD' else 'MXN'
            for l in draft['lineas']:
                product = env['product.product'].browse(l['product_id']).exists()
                l['precio_lista'] = self._quote_list_price(env, product, draft['moneda']) if product else 0.0
        elif accion == 'cancelar':
            draft = {}
        self._quote_save(conv, draft)
        return self._quote_view(env, draft)

    def _t_crear_cotizacion(self, env, args):
        conv, draft = self._quote_draft()
        if conv is None:
            return draft
        if not draft.get('partner_id'):
            return {'error': 'el borrador no tiene cliente'}
        lineas = draft.get('lineas') or []
        if not lineas:
            return {'error': 'el borrador no tiene materiales'}
        for i, l in enumerate(lineas, 1):
            if not (l.get('precio') or l.get('precio_lista')):
                return {'error': 'el renglón %s (%s) no tiene precio ni precio de lista: dicta un precio' % (i, l.get('producto'))}
        cur = draft.get('moneda') or 'MXN'
        Pricelist = env['product.pricelist']
        pl = Pricelist.search([('currency_id.name', '=', cur), ('company_id', 'in', [env.company.id, False])],
                              order='company_id desc', limit=1)
        vals = {
            'partner_id': draft['partner_id'],
            'company_id': env.company.id,
            'origin': 'Asistente IA (audio)',
            'order_line': [(0, 0, {
                'product_id': l['product_id'],
                'product_uom_qty': l['cantidad'],
                'price_unit': l.get('precio') or l.get('precio_lista'),
            }) for l in lineas],
        }
        if pl:
            vals['pricelist_id'] = pl.id
        order = env['sale.order'].create(vals)
        try:
            order.message_post(body='Cotización creada por el asistente IA a petición de %s (WhatsApp).' % env.user.name)
        except Exception:  # noqa: BLE001
            pass
        draft['folio'] = order.name
        self._quote_save(conv, draft)
        conv.sudo().write({'last_quote_order_id': order.id})
        view = self._quote_view(env, draft)
        view.update({'folio': order.name, 'total': order.amount_total, 'estado': 'cotización creada (borrador en Odoo)'})
        return view

    def _t_enviar_cotizacion_cliente(self, env, args):
        conv, draft = self._quote_draft()
        if conv is None:
            return draft
        order = conv.sudo().last_quote_order_id
        if not order:
            return {'error': 'aún no hay cotización creada en esta conversación'}
        partner = order.partner_id
        phone = partner.phone or ''
        if not phone:
            return {'error': 'el cliente %s no tiene teléfono registrado' % partner.display_name}
        lines = []
        for l in order.order_line.filtered(lambda x: not x.display_type):
            lines.append('• %s — %.2f m² × %s = %s' % (
                l.product_id.display_name, l.product_uom_qty,
                _fmt_money(l.price_unit, order.currency_id.name), _fmt_money(l.price_subtotal, order.currency_id.name)))
        body = ('Buen día %s, le compartimos su cotización %s de %s:%s%s%sTotal: %s%sQuedamos atentos. Su asesor le dará seguimiento.') % (
            partner.name, order.name, order.company_id.name, chr(10), chr(10).join(lines), chr(10), _fmt_money(order.amount_total, order.currency_id.name), chr(10))
        number = getattr(self, '_ctx_number', None)
        acc = conv.account_id or None
        msg = self.env['whatsapp.message'].sudo().queue(phone=phone, body=body, partner=partner,
                                                        account=acc, res_model='sale.order', res_id=order.id)
        return {'enviado_a': partner.display_name, 'telefono': phone, 'folio': order.name,
                'estado': 'encolado para envío' if msg else 'no se pudo encolar'}

    # ── dirección general ──
    def _t_resumen_ejecutivo(self, env, args):
        if 'som.analytics' not in env:
            return {'error': 'analytics no disponible'}
        try:
            data = env['som.analytics'].get_exec_summary({})
        except Exception as e:  # noqa: BLE001
            return {'error': 'sin acceso al resumen ejecutivo: %s' % str(e)[:120]}
        if isinstance(data, dict) and data.get('error'):
            return {'error': str(data['error'])[:200]}
        return {'resumen_ejecutivo': data}

    def _t_cartera_vencida(self, env, args):
        limit = self._limit(args, default=10, cap=30)
        Move = env['account.move']
        dom = [('move_type', '=', 'out_invoice'), ('state', '=', 'posted'), ('payment_state', 'in', ('not_paid', 'partial')), ('amount_residual', '>', 0)]
        today = fields.Date.context_today(env['res.partner'])
        moves = Move.search(dom)
        por_cliente = {}
        vencido_total = 0.0
        total = 0.0
        for m in moves:
            total += m.amount_residual_signed or m.amount_residual
            overdue = bool(m.invoice_date_due and m.invoice_date_due < today)
            e = por_cliente.setdefault(m.partner_id.id, {'cliente': m.partner_id.display_name, 'vendedor': m.partner_id.user_id.name or '',
                                                        'saldo': 0.0, 'vencido': 0.0, 'facturas_vencidas': 0, 'mas_antigua_dias': 0})
            amt = m.amount_residual_signed or m.amount_residual
            e['saldo'] += amt
            if overdue:
                e['vencido'] += amt
                e['facturas_vencidas'] += 1
                e['mas_antigua_dias'] = max(e['mas_antigua_dias'], (today - m.invoice_date_due).days)
                vencido_total += amt
        rows = sorted(por_cliente.values(), key=lambda r: -r['vencido'])
        rows = [dict(r, saldo=round(r['saldo'], 2), vencido=round(r['vencido'], 2)) for r in rows if r['vencido'] > 0][:limit]
        cur = env.company.currency_id.name
        return {'moneda': cur, 'por_cobrar_total': round(total, 2), 'vencido_total': round(vencido_total, 2), 'clientes_vencidos': rows}

    def _t_caja_del_dia(self, env, args):
        if 'cash.entry' not in env:
            return {'error': 'caja no disponible'}
        periodo = {'dia': 'today', 'semana': 'week', 'mes': 'month'}.get(args.get('periodo') or 'dia', 'today')
        try:
            data = env['cash.entry'].get_dashboard_data(period=periodo)
        except Exception:
            try:
                data = env['cash.entry'].get_dashboard_data(period='month')
            except Exception as e:  # noqa: BLE001
                return {'error': 'sin acceso a caja: %s' % str(e)[:120]}
        keep = {k: v for k, v in (data or {}).items() if not isinstance(v, (list, dict)) or k in ('totals', 'summary', 'balance', 'currency', 'period_label')}
        return {'caja': keep}

    def _t_pedidos_en_riesgo(self, env, args):
        limit = self._limit(args, default=10, cap=30)
        SO = env['sale.order']
        if 'x_flow_status' not in SO._fields:
            return {'error': 'semáforo no disponible'}
        dom = [('state', '=', 'sale'), ('x_flow_status', 'in', ('nopay', 'stalled', 'dead', 'slow'))]
        orders = SO.search(dom, order='x_flow_days desc' if 'x_flow_days' in SO._fields else 'date_order', limit=limit)
        sel = dict(SO._fields['x_flow_status'].selection)
        out = []
        for so in orders:
            row = {'folio': so.name, 'cliente': so.partner_id.name, 'vendedor': so.user_id.name,
                   'semaforo': sel.get(so.x_flow_status, so.x_flow_status), 'total': _fmt_money(so.amount_total, so.currency_id.name)}
            if 'x_flow_days' in SO._fields:
                row['dias'] = so.x_flow_days
            if 'x_flow_paid_pct' in SO._fields:
                row['pagado_pct'] = round(so.x_flow_paid_pct or 0, 1)
            out.append(row)
        return {'pedidos_en_riesgo': out, 'total': SO.search_count(dom)}
