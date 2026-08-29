# SOM WhatsApp (Baileys) — módulo Odoo 19 + gateway

## Arquitectura
- `gateway/`: servicio Node (Baileys) en Docker. API HTTP estándar con `x-api-key`;
  webhooks a Odoo (`/som_whatsapp/webhook`) con `x-webhook-token`.
- Módulo Odoo `som_whatsapp`: Ajustes, Cuentas (QR), Mensajes, Plantillas y **Puntos de conexión**.

## Gateway (servidor)
```
/root/som-whatsapp-gateway   ← docker compose up -d --build
red: recubrimientos_default  → Odoo lo ve como http://som-whatsapp-gateway:3000
.env: API_KEY, WEBHOOK_URL, WEBHOOK_TOKEN, DEFAULT_COUNTRY_CODE
```
Endpoints: `GET /health`, `POST /sessions/:id/start`, `GET /sessions/:id/status`,
`DELETE /sessions/:id`, `POST /sessions/:id/check {phone}`, `POST /sessions/:id/send {to,text}`,
`POST /sessions/:id/send-media {to,caption,mimetype,filename,base64}`.

## Odoo
1. Ajustes › WhatsApp: URL `http://som-whatsapp-gateway:3000`, API key y token (del `.env`), código de país 52. Probar conexión.
2. WhatsApp › Cuentas: crear cuenta (clave `ventas-mty`), **Iniciar / Generar QR**, escanear.
3. Plantillas: texto con `{{ object.campo }}`; opcional PDF de reporte.
4. Puntos de conexión: clave + modelo + plantilla + destinatario. Disparo desde código:
   `self.env['whatsapp.event'].fire('sale.order.confirmed', orders)` o mixin `whatsapp.notify.mixin`.

## Puntos abiertos (siguiente iteración)
- Enganchar procesos: confirmación de venta, apartado, autorización de precios, entregas, cobranza (semáforo), incidencias.
- Ruteo de entrantes: `whatsapp.event._on_inbound(msg)` (respuestas automáticas, asignar a vendedor).
- Multi-cuenta por equipo/sucursal (`whatsapp.event.account_id`).

## Reservas (holds) — integrado
- Vendedor: WhatsApp 2 días antes (`hold.seller_t2`) y el día del vencimiento (`hold.seller_t0`), con job name, cliente, folio, material y liga.
- Cliente: la mañana del vencimiento (`hold.client_expiry`), o la anterior si vence temprano; solo si tiene teléfono.
- Cron horario `WhatsApp: avisos de reservas`; hora de envío en Ajustes › WhatsApp › Reservas. Renovar re-arma los avisos.
