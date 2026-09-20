"""Cierra las actividades "WhatsApp … en pausa" que quedaron colgadas.

Reanudar la cuenta no cerraba la actividad de ningún manager (cada uno
recibió la suya). Desde esta versión Reanudar las cierra; aquí se archivan
las de cuentas que ya no están en pausa.
"""
import logging

from odoo import api, SUPERUSER_ID

_logger = logging.getLogger(__name__)

FEEDBACK = 'Cerrada por limpieza: el documento ya estaba resuelto.'


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    Activity = env['mail.activity'].sudo()
    acts = Activity.search([('res_model', '=', 'whatsapp.account'),
                            ('summary', '=ilike', 'WhatsApp%en pausa')])
    accounts = env['whatsapp.account'].browse(list(set(acts.mapped('res_id')))).exists()
    resumed = {a.id for a in accounts if not a.paused}
    stale = acts.filtered(lambda a: a.res_id in resumed or a.res_id not in accounts.ids)
    if stale:
        stale.write({'active': False, 'feedback': FEEDBACK})
    _logger.info('[som_whatsapp] WhatsApp en pausa: %s actividad(es) colgadas cerradas.', len(stale))
