from odoo import api, fields, models


class ResUsers(models.Model):
    _inherit = 'res.users'

    wa_account_id = fields.Many2one('whatsapp.account', compute='_compute_wa', string='Cuenta WhatsApp propia')
    wa_connected = fields.Boolean(compute='_compute_wa', string='WhatsApp conectado', search='_search_wa_connected',
                                  help='Su teléfono está vinculado y operativo: todo lo suyo sale desde su número.')

    def _compute_wa(self):
        Acc = self.env['whatsapp.account'].sudo()
        for user in self:
            acc = Acc.search([('user_id', '=', user.id)], limit=1)
            user.wa_account_id = acc
            user.wa_connected = bool(acc and acc.is_live and Acc.seller_accounts_enabled())

    def _search_wa_connected(self, operator, value):
        accs = self.env['whatsapp.account'].sudo().search([('user_id', '!=', False), ('state', '=', 'connected'), ('paused', '=', False)])
        ids = accs.mapped('user_id').ids
        want = (operator == '=' and value) or (operator == '!=' and not value)
        return [('id', 'in' if want else 'not in', ids)]

    def action_wa_my_account(self):
        return self.env['whatsapp.account'].action_my_account()
