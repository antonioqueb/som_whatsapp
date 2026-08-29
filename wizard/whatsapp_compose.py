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
    report_id = fields.Many2one('ir.actions.report', string='Documento a enviar',
                                domain="[('model', '=', res_model), ('report_type', '=', 'qweb-pdf')]",
                                help='PDF del registro que va adjunto al mensaje. Vacío = sin documento.')

    DEFAULT_REPORTS = {
        'sale.order': 'sale.action_report_saleorder',
        'stock.lot.hold.order': 'stock_lot_dimensions.action_report_stock_lot_hold_order_summary',
    }
    DEFAULT_TEMPLATES = {
        'sale.order': 'som_whatsapp.wa_template_sale_confirmed',
        'stock.lot.hold.order': 'som_whatsapp.wa_template_hold_document',
    }

    @api.model
    def open_for(self, record, template_xmlid=None, report_xmlid=None, title='Enviar WhatsApp al cliente'):
        return {
            'type': 'ir.actions.act_window', 'name': title, 'res_model': 'whatsapp.compose',
            'view_mode': 'form', 'target': 'new',
            'context': {'active_model': record._name, 'active_id': record.id,
                        'wa_template_xmlid': template_xmlid, 'wa_report_xmlid': report_xmlid},
        }

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
            ctx = self.env.context
            rep = self.env.ref(ctx.get('wa_report_xmlid') or self.DEFAULT_REPORTS.get(model, ''), raise_if_not_found=False)
            if rep and rep._name == 'ir.actions.report' and rep.model == model:
                res['report_id'] = rep.id
            tpl = self.env.ref(ctx.get('wa_template_xmlid') or self.DEFAULT_TEMPLATES.get(model, ''), raise_if_not_found=False)
            if tpl and tpl._name == 'whatsapp.template' and tpl.model_id.model == model and rec.exists():
                res['template_id'] = tpl.id
                res['body'] = self.env['whatsapp.event'].render_template(tpl, rec)
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
                self.body = self.env['whatsapp.event'].render_template(self.template_id, rec)

    def action_send(self):
        self.ensure_one()
        Msg = self.env['whatsapp.message']
        attachments = []
        if self.report_id and self.res_model and self.res_id:
            rec = self.env[self.res_model].browse(self.res_id)
            pdf, _fmt = self.env['ir.actions.report'].sudo()._render_qweb_pdf(self.report_id.report_name, [rec.id])
            import base64
            fname = '%s - %s.pdf' % ((rec.display_name or 'Documento').replace('/', '-'), self.report_id.name)
            attachments.append((fname, base64.b64encode(pdf).decode(), 'application/pdf'))
        attachments += [(a.name, a.datas.decode() if isinstance(a.datas, bytes) else a.datas, a.mimetype) for a in self.attachment_ids]
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
        if self.res_model == 'sale.order' and self.res_id:
            self.env['sale.order'].browse(self.res_id).with_context(tracking_disable=True).write({'x_wa_client_notified': True})
        return {'type': 'ir.actions.client', 'tag': 'display_notification',
                'params': {'title': 'WhatsApp', 'type': 'success', 'message': _('Mensaje enviado a %s') % msg.phone, 'next': {'type': 'ir.actions.act_window_close'}}}
