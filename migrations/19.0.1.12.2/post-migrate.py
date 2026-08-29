"""Vendedores sí reciben aviso 2 días antes: reactivar T-2 y regenerar los
avisos que se descartaron (se re-arma la bandera; el cron los vuelve a crear
con el texto nuevo)."""
from odoo import api, SUPERUSER_ID


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    ev = env.ref('som_whatsapp.wa_event_hold_seller_t2', raise_if_not_found=False)
    if not ev:
        return
    ev.write({'active': True})
    discarded = env['whatsapp.message'].search([('event_id', '=', ev.id), ('state', '=', 'failed'),
                                                ('error', 'ilike', 'Descartado'), ('res_model', '=', 'stock.lot.hold.order')])
    holds = env['stock.lot.hold.order'].browse(discarded.mapped('res_id')).exists()
    holds.with_context(tracking_disable=True).write({'x_wa_seller_t2_sent': False})
    discarded.write({'error': 'Descartado por error de configuración; se regeneró un aviso nuevo.'})
