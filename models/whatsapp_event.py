# -*- coding: utf-8 -*-
"""PUNTOS DE CONEXIÓN — el enlace abierto para el resto del sistema.

Un evento = (clave, modelo, plantilla, cómo obtener destinatario). Cualquier
módulo dispara:  self.env['whatsapp.event'].fire('sale.order.confirmed', orders)
y este modelo resuelve destinatarios, renderiza la plantilla y encola el
mensaje. Sin código adicional se pueden activar/desactivar, cambiar plantilla
o cuenta desde la UI.

Modos de destinatario:
- partner_field: ruta a un res.partner en el registro ('partner_id', 'user_id.partner_id', 'x_architect_id')
- phone_field:   ruta a un Char con teléfono
- fixed:         teléfono fijo (p. ej. un grupo interno / dirección)

Disparo automático (opcional): 'on_create' / 'on_write' vía whatsapp.notify.mixin
en el modelo destino, o llamando fire() desde la lógica del proceso.
"""
import logging

from odoo import models, fields, api, _
from odoo.tools.safe_eval import safe_eval

_logger = logging.getLogger(__name__)


class WhatsappEvent(models.Model):
    _name = 'whatsapp.event'
    _description = 'Punto de conexión WhatsApp'
    _order = 'model_id, name'

    name = fields.Char(required=True)
    key = fields.Char(string='Clave', required=True, index=True,
                      help="Identificador que usa el código para disparar el evento, p. ej. 'sale.order.confirmed'.")
    model_id = fields.Many2one('ir.model', string='Modelo', required=True, ondelete='cascade')
    model_name = fields.Char(related='model_id.model', store=True, readonly=True)
    template_id = fields.Many2one('whatsapp.template', string='Plantilla', required=True,
                                  domain="[('model_id', '=', model_id)]")
    account_id = fields.Many2one('whatsapp.account', string='Cuenta (vacío = por defecto)')
    recipient_mode = fields.Selection([
        ('partner_field', 'Contacto del registro'),
        ('phone_field', 'Campo teléfono del registro'),
        ('fixed', 'Teléfono fijo'),
    ], default='partner_field', required=True)
    recipient_path = fields.Char(string='Ruta del campo', default='partner_id',
                                 help="Ruta con puntos: 'partner_id', 'user_id.partner_id', 'x_architect_id'.")
    fixed_phone = fields.Char(string='Teléfono fijo')
    domain = fields.Char(string='Condición (dominio)', default='[]',
                         help='Solo se envía si el registro cumple este dominio.')
    active = fields.Boolean(default=True)
    send_now = fields.Boolean(string='Enviar de inmediato', default=False,
                              help='Sin esperar al cron (útil para avisos urgentes).')
    trigger = fields.Selection([
        ('manual', 'Desde código (fire)'),
        ('on_create', 'Al crear el registro'),
        ('on_write', 'Al modificar el registro'),
    ], default='manual', required=True,
        help='on_create/on_write requieren que el modelo herede whatsapp.notify.mixin.')
    watch_fields = fields.Char(string='Campos vigilados (on_write)',
                               help="Separados por coma: 'state,date_order'. Vacío = cualquier cambio.")
    sent_count = fields.Integer(compute='_compute_sent_count')
    notes = fields.Text()

    _key_uniq = models.Constraint('unique(key)', 'La clave del punto de conexión debe ser única.')

    def _compute_sent_count(self):
        Msg = self.env['whatsapp.message']
        for ev in self:
            ev.sent_count = Msg.search_count([('event_id', '=', ev.id)])

    # ── resolución ──
    def _recipients(self, record):
        """Lista de (partner, phone)."""
        self.ensure_one()
        if self.recipient_mode == 'fixed':
            return [(self.env['res.partner'], self.fixed_phone or '')]
        target = record
        for part in (self.recipient_path or '').split('.'):
            if not part:
                continue
            target = getattr(target, part, False) if target else False
            if not target:
                break
        if not target:
            return []
        if self.recipient_mode == 'partner_field':
            out = []
            for p in target:
                if p._name != 'res.partner':
                    continue
                out.append((p, p.phone or getattr(p, 'mobile', '') or ''))
            return out
        return [(self.env['res.partner'], str(target))]

    def _matches(self, record):
        self.ensure_one()
        dom = safe_eval(self.domain or '[]')
        if not dom:
            return True
        return bool(record.filtered_domain(dom))

    @api.model
    def fire(self, key, records, extra_ctx=None, force=False):
        """Dispara el evento `key` para `records`. Devuelve los mensajes encolados."""
        Msg = self.env['whatsapp.message'].sudo()
        events = self.sudo().search([('key', '=', key), ('active', '=', True)])
        out = Msg
        for ev in events:
            for record in records:
                if record._name != ev.model_name:
                    continue
                if not force and not ev._matches(record):
                    continue
                body = ev.template_id.render_for(record, extra_ctx)
                attachment = None
                try:
                    attachment = ev.template_id.render_attachment(record)
                except Exception as e:  # noqa: BLE001
                    _logger.warning('[WHATSAPP] adjunto no generado (%s): %s', key, e)
                for partner, phone in ev._recipients(record):
                    if not phone:
                        continue
                    try:
                        out |= Msg.queue(phone=phone, body=body, partner=partner or None,
                                         account=ev.account_id or None, attachment=attachment,
                                         res_model=record._name, res_id=record.id,
                                         event=ev, template=ev.template_id, send_now=ev.send_now)
                    except Exception as e:  # noqa: BLE001
                        _logger.warning('[WHATSAPP] %s → %s no encolado: %s', key, phone, e)
        return out

    @api.model
    def _on_inbound(self, message):
        """Punto abierto: ruteo de mensajes entrantes (respuestas automáticas,
        asignación a vendedor, etc.). Por ahora solo deja rastro."""
        return True

    def action_test_send(self):
        """Envía el evento al ÚLTIMO registro del modelo (prueba rápida)."""
        self.ensure_one()
        rec = self.env[self.model_name].search([], order='id desc', limit=1)
        if not rec:
            return
        msgs = self.fire(self.key, rec, force=True)
        return {
            'type': 'ir.actions.client', 'tag': 'display_notification',
            'params': {'title': _('Prueba de punto de conexión'), 'type': 'success' if msgs else 'warning',
                       'message': _('%d mensaje(s) encolado(s) para %s') % (len(msgs), rec.display_name)},
        }


class WhatsappNotifyMixin(models.AbstractModel):
    """Mixin opcional: al heredarlo, los eventos con trigger on_create / on_write
    del modelo se disparan solos. Punto abierto para la siguiente iteración."""
    _name = 'whatsapp.notify.mixin'
    _description = 'Disparo automático de WhatsApp'

    @api.model_create_multi
    def create(self, vals_list):
        recs = super().create(vals_list)
        recs._wa_auto('on_create')
        return recs

    def write(self, vals):
        res = super().write(vals)
        self._wa_auto('on_write', changed=set(vals))
        return res

    def _wa_auto(self, trigger, changed=None):
        Event = self.env['whatsapp.event'].sudo()
        events = Event.search([('model_name', '=', self._name), ('trigger', '=', trigger), ('active', '=', True)])
        for ev in events:
            if trigger == 'on_write' and ev.watch_fields:
                watched = {f.strip() for f in ev.watch_fields.split(',') if f.strip()}
                if changed is not None and not (watched & changed):
                    continue
            Event.fire(ev.key, self)

    def wa_fire(self, key, extra_ctx=None):
        return self.env['whatsapp.event'].fire(key, self, extra_ctx=extra_ctx)
