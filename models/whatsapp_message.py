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
    priority = fields.Integer(default=5, help='9 = urgente (vencimientos del día); se envía primero y también en domingo.')
    scheduled_at = fields.Datetime(string='No antes de', help='Escalonado anti-ráfaga: la cola no lo manda antes de esta hora.')
    from_seller = fields.Boolean(string='Desde teléfono del vendedor', help='Salió (o entró) por la cuenta propia de un vendedor.')
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
        if not self.env.context.get('wa_skip_blocklist') and self.env['whatsapp.blocklist'].is_blocked(phone):
            raise UserError(_('El número %s pidió no recibir WhatsApp de esta cuenta.') % phone)
        if not account:
            origin = None
            if res_model and res_id and res_model in self.env and res_model != self._name:
                origin = self.env[res_model].sudo().browse(res_id).exists()
            account = self.env['whatsapp.account'].for_record(origin) if origin else self.env['whatsapp.account'].get_default_account()
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
            'priority': 9 if (event and (event.key or '').startswith('hold.')) else 5,
            'from_seller': bool(account and account.user_id),
        })
        if send_now:
            msg._send()
        return msg

    def _send(self):
        GW = self.env['whatsapp.gateway']
        for msg in self:
            if msg.direction != 'out' or msg.state not in ('queued', 'failed'):
                continue
            acc = msg.account_id
            if self.env.context.get('wa_account_id'):
                acc = self.env['whatsapp.account'].browse(self.env.context['wa_account_id'])
            body = msg.body or ''
            if not acc or acc.state != 'connected' or acc.paused:
                acc = self.env['whatsapp.account'].get_default_account()  # failover al genérico
                if acc and msg.from_seller and not acc.user_id and msg.res_model and msg.res_id and msg.res_model in self.env:
                    # Iba a salir del teléfono del vendedor y cae al genérico: hay que
                    # decirle al cliente con quién seguir.
                    origin = self.env[msg.res_model].sudo().browse(msg.res_id).exists()
                    if origin:
                        block = self.env['whatsapp.event']._wa_base_ctx(origin).get('seller_block') or ''
                        if block and block not in body:
                            body = (body + '\n' + block).strip()
                    msg.write({'from_seller': False, 'body': body, 'account_id': acc.id})
            if not acc or acc.state != 'connected' or acc.paused:
                msg.write({'state': 'failed', 'error': _('Sin cuenta WhatsApp conectada (o todas en pausa).'), 'retry_count': msg.retry_count + 1})
                continue
            try:
                to = msg.jid or msg.phone
                res = None
                if msg.attachment_id:
                    res = GW.send_media(acc.session_key, to, msg.attachment_id.datas.decode() if isinstance(msg.attachment_id.datas, bytes) else msg.attachment_id.datas,
                                        msg.attachment_id.mimetype, msg.attachment_id.name, body)
                else:
                    res = GW.send_text(acc.session_key, to, body)
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
        """Goteo anti-ráfaga: máximo N por minuto con pausas aleatorias, dentro de
        la ventana horaria, respetando el tope diario (con rampa) por cuenta,
        prioridades y programación; el detector de bloqueos pausa la cuenta."""
        import time
        Policy = self.env['whatsapp.policy']
        Account = self.env['whatsapp.account'].sudo()
        p = Policy.params()
        state, why = Policy.window_state()
        accounts = Account.search([('state', '=', 'connected'), ('paused', '=', False)])
        if p['health_guard']:
            for acc in accounts:
                acc._check_health()
            accounts = accounts.filtered(lambda a: not a.paused)
        if not accounts:
            return True
        now = fields.Datetime.now()
        dom = [('direction', '=', 'out'), '|', ('state', '=', 'queued'),
               '&', ('state', '=', 'failed'), ('retry_count', '<', 3),
               '|', ('scheduled_at', '=', False), ('scheduled_at', '<=', now)]
        if state == 'closed':
            return True
        if state == 'sunday':
            dom.append(('priority', '>=', 9))
        budget = {acc.id: max(0, Policy.daily_cap_for(acc) - Policy.sent_today(acc)) for acc in accounts}
        if not any(budget.values()):
            _logger.info('[WHATSAPP] tope diario alcanzado en todas las cuentas; cola diferida')
            return True
        msgs = self.search(dom, order='priority desc, id asc', limit=limit)
        sent = 0
        for msg in msgs:
            if sent >= p['max_per_minute']:
                break
            acc = msg.account_id if (msg.account_id in accounts) else accounts[0]
            if budget.get(acc.id, 0) <= 0:
                continue
            if msg.error and 'no tiene WhatsApp' in (msg.error or ''):
                msg.write({'retry_count': 3})  # permanente: no reintentar
                continue
            if sent:
                time.sleep(Policy.jitter_seconds())
            msg.with_context(wa_account_id=acc.id)._send()
            if msg.state == 'sent':
                budget[acc.id] -= 1
            sent += 1
            self.env.cr.commit()
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
        origin = None
        if acc.user_id:
            # TELÉFONO DE VENDEDOR: solo se registra lo que viene de clientes
            # conocidos (contacto en Odoo o con envío previo desde esta cuenta).
            # Todo lo demás (chats personales) se descarta sin guardarse.
            from datetime import timedelta
            last = self.sudo().search([('direction', '=', 'out'), ('account_id', '=', acc.id), ('phone', '=', phone),
                                       ('create_date', '>=', fields.Datetime.now() - timedelta(days=90))], order='id desc', limit=1)
            if not partner and not last:
                return None
            if last and last.res_model and last.res_id and last.res_model in self.env and last.res_model != self._name:
                origin = self.env[last.res_model].sudo().browse(last.res_id).exists() or None
        vals = {
            'direction': 'in', 'state': 'received', 'account_id': acc.id,
            'partner_id': partner.id, 'phone': phone, 'jid': data.get('jid'),
            'body': data.get('text') or '', 'wa_message_id': data.get('id'), 'pushname': data.get('pushname') or False,
            'status_date': fields.Datetime.now(), 'from_seller': bool(acc.user_id),
            'res_model': origin._name if origin else False, 'res_id': origin.id if origin else False,
        }
        if data.get('base64'):
            att = self.env['ir.attachment'].sudo().create({
                'name': data.get('filename') or ('media.%s' % (data.get('mimetype') or 'bin').split('/')[-1]),
                'datas': data['base64'], 'mimetype': data.get('mimetype') or 'application/octet-stream',
                'res_model': self._name, 'res_id': 0})
            vals['attachment_id'] = att.id
        msg = self.sudo().create(vals)
        if partner:
            try:
                partner.message_post(body=_('WhatsApp recibido de %s: %s') % (phone, (msg.body or '')[:300]), message_type='notification')
            except Exception:  # noqa: BLE001
                _logger.exception('[WHATSAPP] chatter del contacto no actualizado')
        if acc.user_id:
            # Teléfono de vendedor: es SU chat; sin auto-respuesta ni reenvío. Solo
            # se deja el rastro (y el archivo) en la orden/reserva de origen.
            if origin and hasattr(origin, 'message_post'):
                try:
                    att_copy = msg.attachment_id.copy({'res_model': origin._name, 'res_id': origin.id}) if msg.attachment_id else None
                    origin.message_post(body='WhatsApp del cliente al teléfono de %s: %s' % (acc.user_id.name, (msg.body or '')[:500] or '(archivo adjunto)'),
                                        attachment_ids=[att_copy.id] if att_copy else [])
                except Exception:  # noqa: BLE001
                    _logger.exception('[WHATSAPP] chatter de origen no actualizado')
            return msg
        # Ruteo (número genérico): reenvío al asesor + auto-respuesta. Un fallo
        # aquí se registra pero NO tira el webhook (el mensaje ya quedó en bitácora).
        try:
            self.env['whatsapp.event'].sudo()._on_inbound(msg)
        except Exception as e:  # noqa: BLE001
            _logger.exception('[WHATSAPP] ruteo del entrante %s falló', msg.id)
            msg.write({'error': 'Ruteo falló: %s' % e})
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
