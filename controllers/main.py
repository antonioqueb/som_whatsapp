# -*- coding: utf-8 -*-
"""Webhook del gateway → Odoo. Autenticado por token compartido."""
import json
import logging

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)


class SomWhatsappWebhook(http.Controller):

    @http.route('/som_whatsapp/webhook', type='http', auth='none', methods=['POST'], csrf=False, save_session=False)
    def webhook(self, **kw):
        env = request.env(su=True)
        token = env['ir.config_parameter'].get_param('som_whatsapp.webhook_token', '')
        if not token or request.httprequest.headers.get('x-webhook-token') != token:
            return request.make_json_response({'error': 'token inválido'}, status=401)
        try:
            data = json.loads(request.httprequest.get_data(as_text=True) or '{}')
        except ValueError:
            return request.make_json_response({'error': 'json inválido'}, status=400)
        kind = data.get('type')
        try:
            if kind == 'connection':
                acc = env['whatsapp.account'].search([('session_key', '=', data.get('session'))], limit=1)
                if acc:
                    acc._apply_status({'status': data.get('status'), 'phone': data.get('phone')})
            elif kind == 'message':
                env['whatsapp.message']._inbound_from_webhook(data)
            elif kind == 'status':
                env['whatsapp.message']._status_from_webhook(data)
            else:
                return request.make_json_response({'error': 'tipo desconocido'}, status=400)
        except Exception as e:  # noqa: BLE001
            _logger.exception('[WHATSAPP] webhook %s falló', kind)
            return request.make_json_response({'error': str(e)}, status=500)
        return request.make_json_response({'ok': True})
