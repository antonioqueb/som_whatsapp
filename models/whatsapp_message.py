# -*- coding: utf-8 -*-
"""Bitácora de mensajes WhatsApp. Los salientes nacen en 'queued' y los
envía el cron (o el botón Enviar ahora); los estados llegan por webhook."""
import base64
import logging

from odoo import models, fields, api, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class WhatsappMessage(models.Model):
    _name = 'whatsapp.message'
    _description = 'Mensaje WhatsApp'
    _order = 'create_date desc, id desc'

    direction = fields.Selection([('out', 'Enviado'), ('in', 'Recibido')], required=True, default='out', index=True)
    account_id = fields.Many2one('whatsapp.account', string='Cuenta', index=True, ondelete='set null')
    partner_id = fields.Many2one('res.partner', string='Contacto', index=True)
    phone = fields.Char(string='Teléfono', index=True)
    jid = fields.Char(string='JID WhatsApp', index=True)
    body = fields.Text(string='Mensaje')
    attachment_id = fields.Many2one('ir.attachment', string='Adjunto', ondelete='set null')
    state = fields.Selection([
        ('queued', 'En cola'),
        ('sent', 'Enviado'),
        ('delivered', 'Entregado'),
        ('read', 'Leído'),
        ('failed', 'Fallido'),
        ('received', 'Recibido'),
    ], default='queued', index=True, string='Estado')
    error = fields.Text(readonly=True)
    wa_message_id = fields.Char(string='ID WhatsApp', index=True, readonly=True)
    sent_date = fields.Datetime(readonly=True)
    status_date = fields.Datetime(readonly=True)
    # Origen: registro que disparó el mensaje (punto de conexión / compositor)
    res_model = fields.Char(string='Modelo origen', index=True)
    res_id = fields.Integer(string='ID origen', index=True)
    res_ref = fields.Char(string='Origen', compute='_compute_res_ref')
    event_id = fields.Many2one('whatsapp.event', string='Punto de conexión', ondelete='set null')
    template_id = fields.Many2one('whatsapp.template', string='Plantilla', ondelete='set null')
    user_id = fields.Many2one('res.users', string='Enviado por', default=lambda self: self.env.user)
    company_id = fields.Many2one('res.company', default=lambda self: self.env.company)
    pushname = fields.Char(string='Nombre en WhatsApp', help='Nombre que el remitente muestra en WhatsApp (entrantes).')
    seller_partner_id = fields.Many2one('res.partner', string='Vendedor (seguimiento)', index=True,
                                        help='Entrantes: asesor al que se reenvió el mensaje del cliente.')
    retry_count = fields.Integer(default=0)

    def _compute_res_ref(self):
        for m in self:
            ref = ''
            if m.res_model and m.res_id and m.res_model in self.env:
                rec = self.env[m.res_model].sudo().browse(m.res_id).exists()
                ref = rec.display_name if rec else ''
            m.res_ref = ref

    # ── API de alto nivel (la usan eventos y compositor) ──
    @api.model
    def queue(self, phone=None, body=None, partner=None, account=None, attachment=None,
              res_model=None, res_id=None, event=None, template=None, send_now=False):
        """Encola un mensaje saliente. `attachment` = (nombre, base64, mimetype) o ir.attachment."""
        GW = self.env['whatsapp.gateway']
        phone = GW.normalize_phone(phone or (partner and (partner.phone or getattr(partner, 'mobile', '')) ) or '')
        if not phone:
            raise UserError(_('El contacto no tiene teléfono para WhatsApp.'))
        if partner and getattr(partner, 'whatsapp_opt_out', False):
            raise UserError(_('%s pidió no recibir WhatsApp.') % partner.display_name)
        account = account or self.env['whatsapp.account'].get_default_account()
        att = attachment
        if isinstance(attachment, tuple):
            name, b64, mimetype = attachment
            att = self.env['ir.attachment'].create({
                'name': name, 'datas': b64, 'mimetype': mimetype,
                'res_model': res_model or self._name, 'res_id': res_id or 0})
        msg = self.create({
            'direction': 'out', 'account_id': account.id if account else False,
            'partner_id': partner.id if partner else False, 'phone': phone, 'body': body or '',
            'attachment_id': att.id if att else False, 'res_model': res_model, 'res_id': res_id,
            'event_id': event.id if event else False, 'template_id': template.id if template else False,
        })
        if send_now:
            msg._send()
        return msg

    def _send(self):
        GW = self.env['whatsapp.gateway']
        for msg in self:
            if msg.direction != 'out' or msg.state not in ('queued', 'failed'):
                continue
            acc = msg.account_id or self.env['whatsapp.account'].get_default_account()
            if not acc or acc.state != 'connected':
                msg.write({'state': 'failed', 'error': _('Sin cuenta WhatsApp conectada.'), 'retry_count': msg.retry_count + 1})
                continue
            try:
                to = msg.jid or msg.phone
                res = None
                if msg.attachment_id:
                    res = GW.send_media(acc.session_key, to, msg.attachment_id.datas.decode() if isinstance(msg.attachment_id.datas, bytes) else msg.attachment_id.datas,
                                        msg.attachment_id.mimetype, msg.attachment_id.name, msg.body)
                else:
                    res = GW.send_text(acc.session_key, to, msg.body or '')
                msg.write({
                    'state': 'sent', 'error': False, 'account_id': acc.id,
                    'wa_message_id': res.get('id'), 'jid': res.get('jid') or msg.jid,
                    'sent_date': fields.Datetime.now(),
                })
                msg._post_on_origin(_('WhatsApp enviado a %s: %s') % (msg.phone, (msg.body or '')[:200]))
            except UserError as e:
                msg.write({'state': 'failed', 'error': str(e), 'retry_count': msg.retry_count + 1})
            except Exception as e:  # noqa: BLE001
                _logger.exception('[WHATSAPP] fallo enviando %s', msg.id)
                msg.write({'state': 'failed', 'error': str(e)[:500], 'retry_count': msg.retry_count + 1})

    def _post_on_origin(self, body):
        for msg in self:
            if msg.res_model and msg.res_id and msg.res_model in self.env:
                rec = self.env[msg.res_model].sudo().browse(msg.res_id).exists()
                if rec and hasattr(rec, 'message_post'):
                    rec.message_post(body=body, message_type='notification')

    def action_send_now(self):
        self.filtered(lambda m: m.direction == 'out' and m.state in ('queued', 'failed'))._send()

    def action_retry(self):
        self.write({'state': 'queued', 'error': False})
        self._send()

    @api.model
    def _cron_send_queue(self, limit=50):
        msgs = self.search([('direction', '=', 'out'), ('state', '=', 'queued')], limit=limit, order='id asc')
        msgs._send()
        # Reintento automático de fallidos recientes (máx. 3 intentos)
        retry = self.search([('direction', '=', 'out'), ('state', '=', 'failed'), ('retry_count', '<', 3)], limit=limit)
        retry._send()
        return True

    # ── entrada por webhook ──
    @api.model
    def _inbound_from_webhook(self, data):
        # Baileys puede entregar el mismo mensaje dos veces (reintentos/sync):
        # el id de WhatsApp es la llave.
        if data.get('id'):
            dup = self.sudo().search([('direction', '=', 'in'), ('wa_message_id', '=', data['id'])], limit=1)
            if dup:
                return dup
        acc = self.env['whatsapp.account'].sudo().search([('session_key', '=', data.get('session'))], limit=1)
        phone = self.env['whatsapp.gateway'].normalize_phone(data.get('from') or '')
        partner = self.env['res.partner'].sudo().search([('phone', 'ilike', phone[-10:])], limit=1) if phone else self.env['res.partner']
        vals = {
            'direction': 'in', 'state': 'received', 'account_id': acc.id,
            'partner_id': partner.id, 'phone': phone, 'jid': data.get('jid'),
            'body': data.get('text') or '', 'wa_message_id': data.get('id'), 'pushname': data.get('pushname') or False,
            'status_date': fields.Datetime.now(),
        }
        if data.get('base64'):
            att = self.env['ir.attachment'].sudo().create({
                'name': data.get('filename') or ('media.%s' % (data.get('mimetype') or 'bin').split('/')[-1]),
                'datas': data['base64'], 'mimetype': data.get('mimetype') or 'application/octet-stream',
                'res_model': self._name, 'res_id': 0})
            vals['attachment_id'] = att.id
        msg = self.sudo().create(vals)
        if partner:
            partner.message_post(body=_('WhatsApp recibido de %s: %s') % (phone, (msg.body or '')[:300]), message_type='notification')
        # Punto abierto: aquí se enganchan respuestas automáticas / ruteo.
        self.env['whatsapp.event'].sudo()._on_inbound(msg)
        return msg

    @api.model
    def _status_from_webhook(self, data):
        msg = self.sudo().search([('wa_message_id', '=', data.get('id'))], limit=1)
        if not msg:
            return False
        st = data.get('status')
        mapping = {'sent': 'sent', 'delivered': 'delivered', 'read': 'read', 'played': 'read', 'failed': 'failed'}
        new = mapping.get(st)
        order = ['queued', 'sent', 'delivered', 'read']
        if new and (new == 'failed' or msg.state not in order or order.index(new) > order.index(msg.state) if new in order else True):
            msg.write({'state': new, 'status_date': fields.Datetime.now()})
        return True
