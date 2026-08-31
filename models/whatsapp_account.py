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
    company_id = fields.Many2one('res.company', string='Compañía', index=True, default=lambda self: self.env.company,
                                 help='Número genérico de ESTA compañía. Vacío = compartida (los teléfonos de vendedor van sin compañía).')
    message_count = fields.Integer(compute='_compute_message_count')
    notes = fields.Text()

    _session_key_uniq = models.Constraint('unique(session_key)', 'Ya existe una cuenta con esa clave de sesión.')
    _user_uniq = models.Constraint('unique(user_id)', 'Ese usuario ya tiene una cuenta de WhatsApp ligada.')

    user_id = fields.Many2one('res.users', string='Vendedor (teléfono propio)', index=True, tracking=True,
                              help='Vacío = número genérico de avisos. Con vendedor = su propio teléfono: todo lo suyo sale desde ahí, '
                                   'sin aviso de "seguimiento con su asesor", y sus entrantes de clientes se registran en Odoo sin auto-respuesta.')
    kind = fields.Selection([('generic', 'Genérico (avisos)'), ('seller', 'Teléfono de vendedor')], compute='_compute_kind', store=True)
    is_live = fields.Boolean(compute='_compute_is_live', string='Operativa')
    is_manager = fields.Boolean(compute='_compute_is_manager')

    def _compute_is_manager(self):
        ok = self.env.user.has_group('som_whatsapp.group_whatsapp_manager')
        for rec in self:
            rec.is_manager = ok

    @api.depends('user_id')
    def _compute_kind(self):
        for rec in self:
            rec.kind = 'seller' if rec.user_id else 'generic'

    @api.depends('state', 'paused', 'active')
    def _compute_is_live(self):
        for rec in self:
            rec.is_live = bool(rec.active and rec.state == 'connected' and not rec.paused)

    @api.model
    def seller_accounts_enabled(self):
        return (self.env['ir.config_parameter'].sudo().get_param('som_whatsapp.seller_accounts', 'True') or 'True') not in ('False', '0', '')

    @api.model
    def for_user(self, user):
        """Cuenta viva del vendedor, si la función está activa."""
        if not user or not self.seller_accounts_enabled():
            return self.browse()
        return self.sudo().search([('user_id', '=', user.id), ('state', '=', 'connected'), ('paused', '=', False)], limit=1)

    @api.model
    def for_record(self, record):
        """Ruteo: teléfono del vendedor responsable si está conectado; si no, el
        genérico de la compañía del documento."""
        company = None
        if record is not None and record:
            user = getattr(record, 'user_id', False)
            if user and user._name == 'res.users':
                acc = self.for_user(user)
                if acc:
                    return acc
            company = self._company_of(record)
        return self.get_default_account(company=company)

    @api.model
    def _company_of(self, record):
        """res.company del documento (o vacío si el modelo no la trae)."""
        company = getattr(record, 'company_id', False) if record is not None and record else False
        if company and getattr(company, '_name', '') == 'res.company':
            return company[:1]
        return self.env['res.company']

    @api.model
    def action_my_account(self):
        """Menú 'Mi WhatsApp': abre (o crea) la cuenta del usuario actual."""
        user = self.env.user
        # active_test=False: la unicidad por usuario cuenta también las cuentas
        # ARCHIVADAS; sin esto, con una cuenta archivada el botón intentaba
        # crear otra y reventaba con UniqueViolation. Se reutiliza y desarchiva.
        acc = self.sudo().with_context(active_test=False).search([('user_id', '=', user.id)], limit=1)
        if acc and not acc.active:
            acc.write({'active': True})
        if not acc:
            key = re.sub(r'[^a-z0-9-]+', '-', (user.login or 'u').split('@')[0].lower()).strip('-') or 'u'
            key = 'v-%s-%d' % (key[:20], user.id)
            base, n = key, 1
            while self.sudo().with_context(active_test=False).search_count([('session_key', '=', key)]):
                n += 1
                key = '%s-%d' % (base, n)  # la clave anterior puede pertenecer a una cuenta reconvertida/archivada
            # Teléfono de vendedor: por usuario, sin compañía (lo ve desde cualquiera).
            acc = self.sudo().create({'name': 'WhatsApp de %s' % user.name, 'session_key': key, 'user_id': user.id,
                                      'is_default': False, 'company_id': False})
        return {'type': 'ir.actions.act_window', 'res_model': 'whatsapp.account', 'res_id': acc.id,
                'view_mode': 'form', 'target': 'current', 'name': 'Mi WhatsApp'}

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
    def get_default_account(self, company=None):
        """Failover: la predeterminada si está viva; si no, cualquier otra cuenta
        conectada y sin pausa; al final la predeterminada aunque esté caída.
        Multiempresa: primero el genérico de `company` (la del documento; por
        defecto la activa) o compartido; si no hay, cualquier genérico (como hoy)."""
        # Solo cuentas GENÉRICAS: el teléfono de un vendedor jamás manda lo de otros.
        live = [('user_id', '=', False), ('state', '=', 'connected'), ('paused', '=', False)]
        company = company or self.env.company
        if company:
            own = [('company_id', 'in', [company.id, False])]
            acc = (self.search([('is_default', '=', True)] + own + live, limit=1)
                   or self.search(own + live, order='sequence, id', limit=1))
            if acc:
                return acc
        return (self.search([('is_default', '=', True)] + live, limit=1)
                or self.search(live, order='sequence, id', limit=1)
                or self.search([('is_default', '=', True), ('user_id', '=', False)], limit=1))

    @api.model
    def _som_init_multi_company(self):
        """Data hook (idempotente): los teléfonos de vendedor no son de una
        compañía; se les quita la que heredaron del default."""
        accs = self.sudo().with_context(active_test=False).search([('user_id', '!=', False), ('company_id', '!=', False)])
        if accs:
            accs.write({'company_id': False})
        return True

    paused = fields.Boolean(string='Envíos en pausa', tracking=True,
                            help='La cola no manda por esta cuenta. Lo activa el detector de bloqueos o un administrador.')
    pause_reason = fields.Char(string='Motivo de la pausa')
    first_connected = fields.Date(string='Primera conexión', help='Arranca la rampa de calentamiento (20/50/100 por día en las primeras semanas).')
    sent_today = fields.Integer(compute='_compute_quota', string='Enviados hoy')
    daily_cap_effective = fields.Integer(compute='_compute_quota', string='Tope de hoy')

    def _compute_quota(self):
        Policy = self.env['whatsapp.policy']
        for rec in self:
            rec.sent_today = Policy.sent_today(rec) if rec.id else 0
            rec.daily_cap_effective = Policy.daily_cap_for(rec)

    def action_resume(self):
        self.write({'paused': False, 'pause_reason': False})
        for rec in self:
            rec.message_post(body='Envíos reanudados por %s.' % self.env.user.name)
        return self._reload()

    def _pause(self, reason):
        for rec in self:
            if rec.paused:
                continue
            rec.write({'paused': True, 'pause_reason': reason})
            rec.message_post(body='⛔ Envíos PAUSADOS automáticamente: %s. Revisa el teléfono (¿restricción de WhatsApp?) y pulsa Reanudar.' % reason)
            group = self.env.ref('som_whatsapp.group_whatsapp_manager', raise_if_not_found=False)
            for user in ((group.all_user_ids if group else self.env['res.users']) | rec.user_id):
                try:
                    rec.activity_schedule('mail.mail_activity_data_todo', user_id=user.id,
                                          summary='WhatsApp %s en pausa' % rec.name, note=reason)
                except Exception:  # noqa: BLE001
                    pass

    def _check_health(self):
        """Detector de bloqueos: fallos consecutivos o mensajes que nunca se entregan."""
        from datetime import timedelta
        Msg = self.env['whatsapp.message'].sudo()
        for rec in self:
            last = Msg.search([('direction', '=', 'out'), ('account_id', '=', rec.id),
                               ('state', 'in', ('sent', 'delivered', 'read', 'failed'))], order='id desc', limit=10)
            if len(last) < 6:
                continue
            recent6 = last[:6]
            if sum(1 for m in recent6 if m.state == 'failed') >= 5:
                rec._pause('5 de los últimos 6 envíos fallaron')
                continue
            older = [m for m in last[:8] if m.sent_date and m.sent_date < fields.Datetime.now() - timedelta(minutes=30)]
            if len(older) >= 6 and all(m.state == 'sent' for m in older):
                rec._pause('%d mensajes enviados hace más de 30 min sin llegar a entregados' % len(older))


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
                if not rec.first_connected:
                    vals['first_connected'] = fields.Date.context_today(rec)
            rec.write(vals)

    def action_start(self):
        GW = self.env['whatsapp.gateway']
        for rec in self:
            rec._apply_status(GW._request('POST', '/sessions/%s/start' % rec.session_key, {'mark_read': not rec.user_id}))
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
