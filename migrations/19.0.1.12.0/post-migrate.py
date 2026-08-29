"""Reservas: un solo aviso el día del vencimiento; copy con aire. Reescribe las
plantillas (noupdate), desactiva el evento T-2 y descarta sus mensajes en cola."""
from odoo import api, SUPERUSER_ID


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    from odoo.addons.som_whatsapp.models.wa_copy import BY_XMLID
    for xmlid, body in BY_XMLID.items():
        tpl = env.ref(xmlid, raise_if_not_found=False)
        if tpl:
            tpl.write({'body': body})
    ev = env.ref('som_whatsapp.wa_event_hold_seller_t2', raise_if_not_found=False)
    if ev:
        queued = env['whatsapp.message'].search([('event_id', '=', ev.id), ('state', '=', 'queued')])
        queued.write({'state': 'failed', 'error': 'Descartado: el aviso de 2 días antes se eliminó (solo se avisa el día del vencimiento).', 'retry_count': 3})
        ev.write({'active': False})
