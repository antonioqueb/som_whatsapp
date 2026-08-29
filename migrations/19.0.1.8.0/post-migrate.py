"""Copy nuevo pedido por el cliente (29 ago 2026): aviso 'no atendido' solo en la
auto-respuesta; notificaciones cortas. Las plantillas son noupdate, así que se
reescriben aquí."""
from odoo import api, SUPERUSER_ID


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    from odoo.addons.som_whatsapp.models.wa_copy import BY_XMLID
    for xmlid, body in BY_XMLID.items():
        tpl = env.ref(xmlid, raise_if_not_found=False)
        if tpl:
            tpl.write({'body': body})
    env['ir.config_parameter'].sudo().set_param('som_whatsapp.notice_text', '')
