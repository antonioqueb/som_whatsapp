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
                ctx = self._wa_base_ctx(record)
                ctx.update(extra_ctx or {})
                body = ev.template_id.render_for(record, ctx)
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

    # ── contexto común para TODAS las plantillas ──
    DEFAULT_NOTICE = 'Este número es exclusivo para notificaciones automáticas y no es atendido.'

    @api.model
    def _wa_link(self, phone, text=''):
        phone = self.env['whatsapp.gateway'].normalize_phone(phone or '')
        if not phone:
            return ''
        from urllib.parse import quote
        return 'https://wa.me/%s%s' % (phone, ('?text=' + quote(text)) if text else '')

    @api.model
    def _wa_seller_partner(self, record):
        """Asesor responsable del registro: user_id (venta, reserva…),
        seller_partner_id (mensajes entrantes) o el vendedor del contacto."""
        if record._name == 'whatsapp.message':
            return record.seller_partner_id
        user = getattr(record, 'user_id', False)
        if user and user._name == 'res.users' and user.partner_id:
            return user.partner_id
        partner = getattr(record, 'partner_id', False)
        if partner and getattr(partner, 'user_id', False):
            return partner.user_id.partner_id
        return self.env['res.partner']

    @api.model
    def _wa_base_ctx(self, record):
        """Claves disponibles en cualquier plantilla vía ctx: client, client_phone,
        client_link, seller, seller_phone, seller_link, seller_block, notice, ref, job_suffix."""
        P = self.env['ir.config_parameter'].sudo()
        GW = self.env['whatsapp.gateway']
        partner = getattr(record, 'partner_id', False)
        if partner and partner._name != 'res.partner':
            partner = False
        client_phone = GW.normalize_phone((partner and partner.phone) or (record._name == 'whatsapp.message' and record.phone) or '')
        ref = record.display_name or ''
        if record._name == 'whatsapp.message' and record.res_model and record.res_id:
            origin = self.env[record.res_model].sudo().browse(record.res_id).exists()
            ref = origin.display_name if origin else ref
        seller_partner = self._wa_seller_partner(record)
        seller_phone = GW.normalize_phone(seller_partner.phone or '') if seller_partner else ''
        seller_name = seller_partner.name or '' if seller_partner else ''
        seller_link = self._wa_link(seller_phone, 'Hola, le escribo por %s.' % ref) if seller_phone else ''
        if seller_name and seller_link:
            seller_block = 'Seguimiento y pagos con su asesor *%s*: %s' % (seller_name, seller_link)
        elif seller_name:
            seller_block = 'Su asesor *%s* le dará seguimiento.' % seller_name
        else:
            seller_block = 'Un asesor le dará seguimiento.'
        job = ''
        for f in ('x_project_id', 'project_id'):
            v = getattr(record, f, False)
            if v and getattr(v, 'name', ''):
                job = v.name
                break
        return {
            'ref': ref,
            'client': (partner and partner.display_name) or (record._name == 'whatsapp.message' and record.pushname) or '',
            'client_phone': client_phone,
            'client_link': self._wa_link(client_phone) if client_phone else '',
            'seller': seller_name,
            'seller_phone': seller_phone,
            'seller_link': seller_link,
            'seller_block': seller_block,
            'notice': P.get_param('som_whatsapp.notice_text') or self.DEFAULT_NOTICE,
            'job': job or '—',
            'job_suffix': (' del proyecto *%s*' % job) if job else '',
        }

    # ── entrantes: este número solo notifica; el seguimiento es con el asesor ──
    @api.model
    def _resolve_inbound_origin(self, message):
        """(registro origen, asesor) para un mensaje entrante: el último saliente
        a ese teléfono con registro origen manda; si no, el vendedor del contacto."""
        Msg = self.env['whatsapp.message'].sudo()
        phone = message.phone or ''
        dom = [('direction', '=', 'out'), ('phone', '=', phone), ('res_model', '!=', False), ('res_id', '!=', 0)]
        last = Msg.search(dom + [('res_model', '!=', 'whatsapp.message')], order='id desc', limit=1)
        origin = self.env['res.partner']
        if last and last.res_model in self.env:
            origin = self.env[last.res_model].sudo().browse(last.res_id).exists()
        seller = self._wa_seller_partner(origin) if origin else self.env['res.partner']
        if not seller and message.partner_id and message.partner_id.user_id:
            seller = message.partner_id.user_id.partner_id
        return origin, seller

    @api.model
    def _on_inbound(self, message):
        """Cliente escribe al número notificador → (1) se reenvía al asesor desde
        este mismo número, con adjunto si lo traía; (2) al cliente se le responde
        (con enfriamiento) que este número no se atiende y que el seguimiento es
        con su asesor, con liga directa a su chat."""
        if not message.phone:
            return False
        P = self.env['ir.config_parameter'].sudo()
        Msg = self.env['whatsapp.message'].sudo()
        # Un usuario interno (vendedor contestando un reenvío) no es un cliente.
        internal = self.env['res.users'].sudo().search(
            [('share', '=', False), ('active', '=', True), ('partner_id.phone', 'ilike', message.phone[-10:])], limit=1)
        if internal:
            return False
        origin, seller = self._resolve_inbound_origin(message)
        vals = {'seller_partner_id': seller.id if seller else False}
        if origin:
            vals.update({'res_model': origin._name, 'res_id': origin.id})
        message.write(vals)
        body_txt = (message.body or '').strip() or '[archivo adjunto sin texto]'
        if origin and hasattr(origin, 'message_post'):
            origin.message_post(body='WhatsApp del cliente al número notificador (%s): %s%s' % (
                message.phone, body_txt[:500], ' · se reenvió a %s' % seller.name if seller else ' · SIN asesor para reenviar'))
        ctx = {'client_text': body_txt[:1500]}
        # (1) reenvío al asesor (o al número de respaldo)
        forwarded = Msg
        if seller and seller.phone:
            forwarded = self.fire('inbound.forward_seller', message, extra_ctx=ctx)
        else:
            fallback = P.get_param('som_whatsapp.fallback_forward_phone') or ''
            if fallback:
                base = self._wa_base_ctx(message)
                base.update(ctx)
                text = ('📨 *Mensaje de cliente sin asesor asignado*\n👤 %s · %s\n📋 Ref: %s\n💬 "%s"\n\n'
                        'Asigna un vendedor y contáctalo: %s') % (
                    base['client'] or 'Desconocido', base['client_phone'], base['ref'] or '—', base['client_text'], base['client_link'])
                try:
                    forwarded = Msg.queue(phone=fallback, body=text, res_model='whatsapp.message', res_id=message.id)
                except Exception as e:  # noqa: BLE001
                    _logger.warning('[WHATSAPP] reenvío de respaldo no encolado: %s', e)
        if forwarded and message.attachment_id:
            for fw in forwarded:
                try:
                    Msg.queue(phone=fw.phone, body='📎 Adjunto del cliente %s' % (message.partner_id.display_name or message.phone),
                              partner=fw.partner_id or None, attachment=message.attachment_id,
                              res_model='whatsapp.message', res_id=message.id, send_now=False)
                except Exception as e:  # noqa: BLE001
                    _logger.warning('[WHATSAPP] adjunto no reenviado: %s', e)
        # (2) auto-respuesta al cliente, una vez por ventana de enfriamiento
        try:
            hours = int(P.get_param('som_whatsapp.autoreply_cooldown_hours', '12') or 12)
        except ValueError:
            hours = 12
        from datetime import timedelta
        ev = self.sudo().search([('key', '=', 'inbound.client_autoreply')], limit=1)
        recent = Msg.search_count([('direction', '=', 'out'), ('phone', '=', message.phone), ('event_id', '=', ev.id),
                                   ('create_date', '>=', fields.Datetime.now() - timedelta(hours=hours))]) if ev else 0
        if not recent:
            self.fire('inbound.client_autoreply', message, extra_ctx=ctx)
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
