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
        # OJO: otros módulos hacen sale→draft→sale como estado transitorio (cambio
        # de lista de precios en inventory_shopping_cart, con tracking_disable);
        # eso NO es una confirmación: la bandera y el candado temporal lo evitan.
        to_notify = self.env['sale.order']
        if vals.get('state') == 'sale' and not self.env.context.get('tracking_disable'):
            to_notify = self.filtered(lambda o: o.state != 'sale' and not o.x_wa_client_notified)
        res = super().write(vals)
        if to_notify:
            to_notify = to_notify.filtered(lambda o: o.state == 'sale' and not o.x_wa_client_notified)
            to_notify._wa_notify_client_confirmed()
        return res

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

    def action_wa_send_confirmation(self):
        """Botón: (re)enviar al cliente el aviso de confirmación con el PDF."""
        self._wa_notify_client_confirmed(force=True)
        return {'type': 'ir.actions.client', 'tag': 'display_notification',
                'params': {'title': 'WhatsApp', 'type': 'success',
                           'message': 'Aviso de confirmación enviado al cliente.'}}
