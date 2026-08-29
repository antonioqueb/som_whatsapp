# -*- coding: utf-8 -*-
"""Cliente del gateway Baileys (API HTTP estándar). Único punto que habla
HTTP: cuentas, mensajes y eventos pasan por aquí."""
import logging
import re

import requests

from odoo import models, api, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

PARAM_URL = 'som_whatsapp.gateway_url'
PARAM_KEY = 'som_whatsapp.api_key'
PARAM_WEBHOOK_TOKEN = 'som_whatsapp.webhook_token'
PARAM_CC = 'som_whatsapp.default_country_code'
PARAM_TIMEOUT = 'som_whatsapp.timeout'


class WhatsappGateway(models.AbstractModel):
    _name = 'whatsapp.gateway'
    _description = 'Gateway WhatsApp (Baileys)'

    @api.model
    def _config(self):
        P = self.env['ir.config_parameter'].sudo()
        try:
            timeout = float(P.get_param(PARAM_TIMEOUT, '20') or 20)
        except (TypeError, ValueError):
            timeout = 20.0
        return {
            'url': (P.get_param(PARAM_URL, '') or '').rstrip('/'),
            'api_key': P.get_param(PARAM_KEY, '') or '',
            'webhook_token': P.get_param(PARAM_WEBHOOK_TOKEN, '') or '',
            'cc': (P.get_param(PARAM_CC, '52') or '52').strip(),
            'timeout': timeout,
        }

    @api.model
    def _request(self, method, path, payload=None, raise_on_error=True):
        cfg = self._config()
        if not cfg['url'] or not cfg['api_key']:
            raise UserError(_('Configura la URL del gateway y la API key en Ajustes › WhatsApp.'))
        url = cfg['url'] + path
        try:
            resp = requests.request(
                method, url, json=payload, timeout=cfg['timeout'],
                headers={'x-api-key': cfg['api_key'], 'content-type': 'application/json'})
        except requests.RequestException as e:
            if raise_on_error:
                raise UserError(_('No se pudo contactar al gateway WhatsApp (%s): %s') % (url, e))
            return {'error': str(e)}
        try:
            data = resp.json()
        except ValueError:
            data = {'error': resp.text[:300]}
        if resp.status_code >= 400:
            msg = data.get('error') or resp.text[:300]
            if raise_on_error:
                raise UserError(_('Gateway WhatsApp: %s') % msg)
            return {'error': msg}
        return data

    # ── utilidades ──
    @api.model
    def normalize_phone(self, raw):
        """Dígitos en formato internacional sin '+'. 10 dígitos → código de
        país por defecto (52); '521…' legado → '52…'."""
        d = re.sub(r'\D', '', raw or '')
        if not d:
            return ''
        cc = self._config()['cc']
        if len(d) == 10:
            d = cc + d
        if d.startswith('521') and len(d) == 13:
            d = '52' + d[3:]
        return d

    @api.model
    def health(self):
        return self._request('GET', '/health')

    @api.model
    def check_number(self, session_key, phone):
        return self._request('POST', '/sessions/%s/check' % session_key, {'phone': self.normalize_phone(phone)})

    @api.model
    def send_text(self, session_key, to, text):
        return self._request('POST', '/sessions/%s/send' % session_key, {'to': to, 'text': text})

    @api.model
    def send_media(self, session_key, to, base64_data, mimetype, filename=None, caption=None):
        return self._request('POST', '/sessions/%s/send-media' % session_key, {
            'to': to, 'base64': base64_data, 'mimetype': mimetype,
            'filename': filename or 'archivo', 'caption': caption or ''})
