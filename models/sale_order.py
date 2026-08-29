import logging

from odoo import fields, models

_logger = logging.getLogger(__name__)

KEY_ORDER_CONFIRMED = 'sale.order.confirmed'


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    x_wa_client_notified = fields.Boolean(string='WhatsApp de confirmación enviado', copy=False,
                                          help='Se marca al enviar manualmente la confirmación desde el botón.')

    # Sin envío automático al confirmar (decisión del cliente, 29 ago 2026):
    # el aviso de venta sale SOLO desde el botón "WhatsApp al cliente", donde
    # el vendedor elige el documento. `_wa_notify_client_confirmed` se conserva
    # como punto de conexión por si algún día se quiere reactivar.

    def _wa_recently_notified(self, minutes=10):
        """Candado extra contra duplicados: ya hay un aviso de confirmación de
        esta orden encolado/enviado en los últimos `minutes`."""
        self.ensure_one()
        from datetime import timedelta
        ev = self.env['whatsapp.event'].sudo().search([('key', '=', KEY_ORDER_CONFIRMED)], limit=1)
        if not ev:
            return False
        return bool(self.env['whatsapp.message'].sudo().search_count([
            ('event_id', '=', ev.id), ('res_model', '=', 'sale.order'), ('res_id', '=', self.id),
            ('create_date', '>=', fields.Datetime.now() - timedelta(minutes=minutes))]))

    def _wa_notify_client_confirmed(self, force=False):
        """Punto de conexión `sale.order.confirmed` (PDF adjunto según la
        plantilla). Nunca bloquea la confirmación: cualquier falla queda en el
        log y en el chatter. La bandera se marca ANTES de enviar para que una
        escritura anidada no vuelva a entrar."""
        Event = self.env['whatsapp.event'].sudo()
        for order in self:
            if not force and (order.x_wa_client_notified or order._wa_recently_notified()):
                continue
            order.with_context(tracking_disable=True).write({'x_wa_client_notified': True})
            phone = order.partner_id.phone
            if not phone:
                order.message_post(body='WhatsApp de confirmación omitido: el cliente no tiene teléfono registrado.')
                continue
            try:
                msgs = Event.fire(KEY_ORDER_CONFIRMED, order)
                if msgs:
                    order.message_post(body='WhatsApp de confirmación enviado al cliente (%s). Seguimiento indicado con %s.' % (
                        phone, order.user_id.name or 'el vendedor'))
                else:
                    order.message_post(body='WhatsApp de confirmación NO encolado (sin punto de conexión activo, opt-out o error del gateway).')
            except Exception as e:  # noqa: BLE001
                _logger.exception('[WHATSAPP] confirmación de %s no enviada', order.name)
                order.message_post(body='WhatsApp de confirmación falló: %s' % e)

    def action_wa_open_compose(self):
        """Botón: elegir documento (resumen / detalle / sin precios…) y texto antes de enviar."""
        self.ensure_one()
        return self.env['whatsapp.compose'].open_for(self, 'som_whatsapp.wa_template_sale_confirmed',
                                                     'sale.action_report_saleorder')

    def action_wa_send_confirmation(self):
        """Botón: (re)enviar al cliente el aviso de confirmación con el PDF."""
        self._wa_notify_client_confirmed(force=True)
        return {'type': 'ir.actions.client', 'tag': 'display_notification',
                'params': {'title': 'WhatsApp', 'type': 'success',
                           'message': 'Aviso de confirmación enviado al cliente.'}}
