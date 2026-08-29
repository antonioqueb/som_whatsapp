import logging

from odoo import fields, models

_logger = logging.getLogger(__name__)

KEY_ORDER_CONFIRMED = 'sale.order.confirmed'


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    x_wa_client_notified = fields.Boolean(string='WhatsApp de confirmación enviado', copy=False)

    def write(self, vals):
        # Órdenes que pasan a confirmadas en esta escritura (botón Confirmar o
        # "nace confirmada"): el cliente recibe el aviso sin que nadie haga nada.
        to_notify = self.env['sale.order']
        if vals.get('state') == 'sale':
            to_notify = self.filtered(lambda o: o.state != 'sale' and not o.x_wa_client_notified)
        res = super().write(vals)
        if to_notify:
            to_notify._wa_notify_client_confirmed()
        return res

    def _wa_notify_client_confirmed(self):
        """Punto de conexión `sale.order.confirmed` (PDF adjunto según la
        plantilla). Nunca bloquea la confirmación: cualquier falla queda en el
        log y en el chatter."""
        Event = self.env['whatsapp.event'].sudo()
        for order in self:
            phone = order.partner_id.phone
            if not phone:
                order.x_wa_client_notified = True
                order.message_post(body='WhatsApp de confirmación omitido: el cliente no tiene teléfono registrado.')
                continue
            try:
                msgs = Event.fire(KEY_ORDER_CONFIRMED, order)
                order.x_wa_client_notified = True
                if msgs:
                    order.message_post(body='WhatsApp de confirmación enviado al cliente (%s). Seguimiento indicado con %s.' % (
                        phone, order.user_id.name or 'el vendedor'))
                else:
                    order.message_post(body='WhatsApp de confirmación NO encolado (sin punto de conexión activo, opt-out o error del gateway).')
            except Exception as e:  # noqa: BLE001
                _logger.exception('[WHATSAPP] confirmación de %s no enviada', order.name)
                order.message_post(body='WhatsApp de confirmación falló: %s' % e)

    def action_wa_send_confirmation(self):
        """Botón: (re)enviar al cliente el aviso de confirmación con el PDF."""
        for order in self:
            order.x_wa_client_notified = False
        self._wa_notify_client_confirmed()
        return {'type': 'ir.actions.client', 'tag': 'display_notification',
                'params': {'title': 'WhatsApp', 'type': 'success',
                           'message': 'Aviso de confirmación enviado al cliente.'}}
