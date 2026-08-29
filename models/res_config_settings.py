from odoo import models, fields, _


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    wa_gateway_url = fields.Char(string='URL del gateway', config_parameter='som_whatsapp.gateway_url',
                                 help='Ej. http://som-whatsapp-gateway:3000 (red interna de Docker).')
    wa_api_key = fields.Char(string='API key', config_parameter='som_whatsapp.api_key')
    wa_webhook_token = fields.Char(string='Token de webhook', config_parameter='som_whatsapp.webhook_token',
                                   help='Debe coincidir con WEBHOOK_TOKEN del gateway.')
    wa_default_country_code = fields.Char(string='Código de país por defecto', config_parameter='som_whatsapp.default_country_code', default='52')
    wa_timeout = fields.Char(string='Timeout (s)', config_parameter='som_whatsapp.timeout', default='20')

    def action_wa_test(self):
        h = self.env['whatsapp.gateway'].health()
        return {
            'type': 'ir.actions.client', 'tag': 'display_notification',
            'params': {'title': _('Gateway WhatsApp'), 'type': 'success' if h.get('ok') else 'danger', 'sticky': False,
                       'message': _('Conectado · sesiones: %s') % (', '.join(h.get('sessions') or []) or _('ninguna'))},
        }
