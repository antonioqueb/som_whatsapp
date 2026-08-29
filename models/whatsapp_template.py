# -*- coding: utf-8 -*-
"""Plantillas de WhatsApp: texto plano con marcadores inline
({{ object.name }}, {{ object.partner_id.name }}, {{ user.name }}) renderizados
por mail.render.mixin, el mismo motor que usa Odoo en plantillas de correo."""
from odoo import models, fields, api


class WhatsappTemplate(models.Model):
    _name = 'whatsapp.template'
    _description = 'Plantilla WhatsApp'
    _inherit = ['mail.render.mixin']
    _order = 'name'

    name = fields.Char(required=True)
    model_id = fields.Many2one('ir.model', string='Modelo', required=True, ondelete='cascade',
                               help='Modelo sobre el que se renderiza (object = registro).')
    render_model = fields.Char(related='model_id.model', string='Modelo técnico', store=True, readonly=True)
    body = fields.Text(string='Mensaje', required=True, translate=False,
                       help='Texto plano. Marcadores: {{ object.name }}, {{ object.partner_id.name }}, {{ user.name }}, {{ ctx.get("x") }}.')
    attach_report_id = fields.Many2one('ir.actions.report', string='Adjuntar reporte PDF',
                                       domain="[('model_id', '=', model_id)]",
                                       help='Opcional: PDF del registro que se envía junto al mensaje.')
    active = fields.Boolean(default=True)
    company_id = fields.Many2one('res.company')

    def render_for(self, record, extra_ctx=None):
        """Texto final para `record`. `ctx` disponible en el marcador."""
        self.ensure_one()
        rendered = self.with_context(**(extra_ctx or {}))._render_field(
            'body', [record.id], engine='inline_template',
            add_context={'ctx': extra_ctx or {}}, options={'post_process': False})
        return (rendered.get(record.id) or '').strip()

    def render_attachment(self, record):
        """(nombre, base64, mimetype) del reporte PDF, si la plantilla lo lleva."""
        self.ensure_one()
        if not self.attach_report_id:
            return None
        pdf, _fmt = self.env['ir.actions.report'].sudo()._render_qweb_pdf(self.attach_report_id.report_name, [record.id])
        import base64
        name = '%s.pdf' % (record.display_name or self.attach_report_id.name).replace('/', '-')
        return (name, base64.b64encode(pdf).decode(), 'application/pdf')
