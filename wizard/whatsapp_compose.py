# -*- coding: utf-8 -*-
"""Compositor: enviar WhatsApp desde cualquier registro (acción de servidor
'Enviar WhatsApp' en Contactos y Órdenes de venta; extensible a más modelos)."""
from odoo import models, fields, api, _
from odoo.exceptions import UserError


class WhatsappCompose(models.TransientModel):
    _name = 'whatsapp.compose'
    _description = 'Enviar WhatsApp'

    res_model = fields.Char()
    res_id = fields.Integer()
    partner_id = fields.Many2one('res.partner', string='Contacto')
    phone = fields.Char(string='Teléfono', required=True)
    account_id = fields.Many2one('whatsapp.account', string='Cuenta', default=lambda self: self.env['whatsapp.account'].get_default_account())
    template_id = fields.Many2one('whatsapp.template', string='Plantilla')
    body = fields.Text(string='Mensaje', required=True)
    attachment_ids = fields.Many2many('ir.attachment', string='Adjuntos')

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        model = self.env.context.get('active_model')
        rid = self.env.context.get('active_id')
        if model and rid:
            rec = self.env[model].browse(rid)
            res.update({'res_model': model, 'res_id': rid})
            partner = rec if model == 'res.partner' else getattr(rec, 'partner_id', False)
            if partner:
                res['partner_id'] = partner.id
                res['phone'] = partner.phone or getattr(partner, 'mobile', '') or ''
        return res

    @api.onchange('partner_id')
    def _onchange_partner(self):
        if self.partner_id and not self.phone:
            self.phone = self.partner_id.phone or ''

    @api.onchange('template_id')
    def _onchange_template(self):
        if self.template_id and self.res_model and self.res_id:
            rec = self.env[self.res_model].browse(self.res_id)
            if rec.exists() and self.template_id.model_id.model == self.res_model:
                self.body = self.template_id.render_for(rec)

    def action_send(self):
        self.ensure_one()
        Msg = self.env['whatsapp.message']
        attachments = [(a.name, a.datas.decode() if isinstance(a.datas, bytes) else a.datas, a.mimetype) for a in self.attachment_ids]
        if attachments:
            first = attachments[0]
            msg = Msg.queue(phone=self.phone, body=self.body, partner=self.partner_id or None, account=self.account_id or None,
                            attachment=first, res_model=self.res_model, res_id=self.res_id, template=self.template_id or None, send_now=True)
            for att in attachments[1:]:
                Msg.queue(phone=self.phone, body='', partner=self.partner_id or None, account=self.account_id or None,
                          attachment=att, res_model=self.res_model, res_id=self.res_id, send_now=True)
        else:
            msg = Msg.queue(phone=self.phone, body=self.body, partner=self.partner_id or None, account=self.account_id or None,
                            res_model=self.res_model, res_id=self.res_id, template=self.template_id or None, send_now=True)
        if msg.state == 'failed':
            raise UserError(_('No se pudo enviar: %s') % (msg.error or ''))
        return {'type': 'ir.actions.client', 'tag': 'display_notification',
                'params': {'title': 'WhatsApp', 'type': 'success', 'message': _('Mensaje enviado a %s') % msg.phone, 'next': {'type': 'ir.actions.act_window_close'}}}
