# -*- coding: utf-8 -*-
"""Cuenta WhatsApp = sesión del gateway. Se empareja escaneando el QR desde
esta ficha (el QR lo entrega el gateway y se guarda como imagen)."""
import base64
import re

from odoo import models, fields, api, _
from odoo.exceptions import UserError


class WhatsappAccount(models.Model):
    _name = 'whatsapp.account'
    _description = 'Cuenta WhatsApp (sesión Baileys)'
    _inherit = ['mail.thread']
    _order = 'sequence, id'

    name = fields.Char(required=True, tracking=True)
    session_key = fields.Char(
        string='Clave de sesión', required=True, copy=False,
        help='Identificador de la sesión en el gateway (solo letras, números, guion).')
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    is_default = fields.Boolean(string='Cuenta por defecto', tracking=True)
    state = fields.Selection([
        ('stopped', 'Detenida'),
        ('starting', 'Iniciando'),
        ('qr', 'Esperando QR'),
        ('connected', 'Conectada'),
        ('disconnected', 'Desconectada'),
        ('logged_out', 'Sesión cerrada'),
    ], default='stopped', tracking=True, readonly=True)
    phone = fields.Char(string='Número conectado', readonly=True)
    qr_image = fields.Binary(string='Código QR', attachment=False, readonly=True)
    qr_at = fields.Datetime(string='QR generado', readonly=True)
    last_sync = fields.Datetime(string='Última sincronización', readonly=True)
    company_id = fields.Many2one('res.company', default=lambda self: self.env.company)
    message_count = fields.Integer(compute='_compute_message_count')
    notes = fields.Text()

    _session_key_uniq = models.Constraint('unique(session_key)', 'Ya existe una cuenta con esa clave de sesión.')

    @api.constrains('session_key')
    def _check_session_key(self):
        for rec in self:
            if not re.fullmatch(r'[A-Za-z0-9_-]{2,40}', rec.session_key or ''):
                raise UserError(_('La clave de sesión solo admite letras, números, guion y guion bajo (2-40).'))

    def _compute_message_count(self):
        Msg = self.env['whatsapp.message']
        for rec in self:
            rec.message_count = Msg.search_count([('account_id', '=', rec.id)])

    @api.model
    def get_default_account(self):
        acc = self.search([('is_default', '=', True), ('state', '=', 'connected')], limit=1)
        return acc or self.search([('state', '=', 'connected')], limit=1) or self.search([('is_default', '=', True)], limit=1)

    # ── ciclo de vida de la sesión ──
    def _apply_status(self, data):
        for rec in self:
            vals = {
                'state': data.get('status') or 'stopped',
                'phone': data.get('phone') or rec.phone,
                'last_sync': fields.Datetime.now(),
            }
            qr = data.get('qr')
            if qr and ',' in qr:
                vals['qr_image'] = qr.split(',', 1)[1]
                vals['qr_at'] = fields.Datetime.now()
            elif data.get('status') == 'connected':
                vals['qr_image'] = False
            rec.write(vals)

    def action_start(self):
        GW = self.env['whatsapp.gateway']
        for rec in self:
            rec._apply_status(GW._request('POST', '/sessions/%s/start' % rec.session_key))
        return self._reload()

    def action_refresh(self):
        GW = self.env['whatsapp.gateway']
        for rec in self:
            rec._apply_status(GW._request('GET', '/sessions/%s/status' % rec.session_key))
        return self._reload()

    def action_logout(self):
        GW = self.env['whatsapp.gateway']
        for rec in self:
            GW._request('DELETE', '/sessions/%s' % rec.session_key, raise_on_error=False)
            rec.write({'state': 'logged_out', 'phone': False, 'qr_image': False})
            rec.message_post(body=_('Sesión cerrada desde Odoo.'))
        return self._reload()

    def action_test(self):
        self.ensure_one()
        h = self.env['whatsapp.gateway'].health()
        return {
            'type': 'ir.actions.client', 'tag': 'display_notification',
            'params': {'title': _('Gateway WhatsApp'), 'type': 'success' if h.get('ok') else 'danger',
                       'message': _('Conectado. Sesiones activas: %s') % ', '.join(h.get('sessions') or []) or _('Sin respuesta')},
        }

    def action_view_messages(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window', 'name': _('Mensajes de %s') % self.name,
            'res_model': 'whatsapp.message', 'view_mode': 'list,form',
            'domain': [('account_id', '=', self.id)], 'context': {'default_account_id': self.id},
        }

    def _reload(self):
        return {'type': 'ir.actions.client', 'tag': 'reload'}

    @api.model
    def _cron_sync_status(self):
        for acc in self.search([]):
            try:
                acc._apply_status(self.env['whatsapp.gateway']._request(
                    'GET', '/sessions/%s/status' % acc.session_key, raise_on_error=False) or {})
            except Exception:  # noqa: BLE001
                continue
