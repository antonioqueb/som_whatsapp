# -*- coding: utf-8 -*-
"""Repara plantillas de WhatsApp cuyas expresiones quedaron en MAYÚSCULAS
por el mayúsculas global del backend ({{ OBJECT.NAME }}, CTX.GET('WHEN')):
el motor inline_template las evaluaba como nombres inexistentes y el cron
de avisos de reservas fallaba en todas (RES/00355, 9 sep 2026). Solo se
pasa a minúsculas lo que está DENTRO de {{ }}; el texto libre se respeta."""
import logging
import re

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)

EXPR = re.compile(r'\{\{(.*?)\}\}', re.S)


def _fix(body):
    return EXPR.sub(lambda m: '{{' + m.group(1).lower() + '}}', body or '')


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    Template = env['whatsapp.template'].with_context(active_test=False)
    fixed = 0
    for tpl in Template.search([]):
        body = tpl.body or ''
        new = _fix(body)
        if new != body:
            tpl.write({'body': new})
            fixed += 1
            _logger.info('[WA TEMPLATES] Plantilla %s (%s) normalizada.', tpl.id, tpl.name)
    _logger.info('[WA TEMPLATES] %s plantilla(s) reparadas.', fixed)
