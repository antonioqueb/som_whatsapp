from odoo import models, fields


class ResPartner(models.Model):
    _inherit = 'res.partner'

    whatsapp_opt_out = fields.Boolean(string='No enviar WhatsApp')
    whatsapp_message_ids = fields.One2many('whatsapp.message', 'partner_id', string='WhatsApp')
    whatsapp_message_count = fields.Integer(compute='_compute_whatsapp_message_count')

    def _compute_whatsapp_message_count(self):
        Msg = self.env['whatsapp.message']
        for p in self:
            p.whatsapp_message_count = Msg.search_count([('partner_id', '=', p.id)])

    def action_view_whatsapp(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window', 'name': 'WhatsApp · %s' % self.display_name,
            'res_model': 'whatsapp.message', 'view_mode': 'list,form',
            'domain': [('partner_id', '=', self.id)], 'context': {'default_partner_id': self.id},
        }
