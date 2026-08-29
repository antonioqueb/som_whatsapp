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

## Venta confirmada y seguimiento con el asesor (número intermediario)
- Venta: envío MANUAL desde el botón "WhatsApp al cliente" (asistente: documento a adjuntar + texto). No hay envío automático al confirmar. Al cliente se le indica nombre y número de su asesor (sin ligas).
- Toda plantilla dispone de `ctx.seller_block`, `ctx.notice`, `ctx.seller_link`, `ctx.client_link`, `ctx.ref`, `ctx.job_suffix`.
- Entrantes (`whatsapp.event._on_inbound`): el mensaje del cliente se reenvía al asesor desde el mismo número (`inbound.forward_seller`, adjuntos incluidos) y al cliente se le responde una vez por ventana (`inbound.client_autoreply`) reiterando que el seguimiento es con su asesor. Usuarios internos no reciben auto-respuesta. Sin asesor → número de respaldo (Ajustes).
- Legado eliminado: wizard `som.whatsapp.send` (hoja nativa de compartir) de inventory_shopping_cart.

## Anti-baneo (Ajustes › WhatsApp › Anti-baneo)
Goteo con jitter y "escribiendo…", tope diario con rampa de calentamiento por cuenta, ventana horaria (domingo solo urgentes), avisos de reservas escalonados, variantes de texto `{a|b}`, baja inmediata (BAJA/STOP → lista de baja), detector de bloqueos (pausa la cuenta y avisa), failover automático a otra cuenta conectada.
