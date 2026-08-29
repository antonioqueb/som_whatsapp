"""Política anti-baneo (capas 1–7 y 10):
goteo con jitter, tope diario con rampa de calentamiento, ventana horaria,
prioridades, variación de texto, baja inmediata, detector de bloqueos y
failover entre cuentas. Todo configurable en Ajustes › WhatsApp."""
import random
import re
import unicodedata
from datetime import timedelta

import pytz

from odoo import api, fields, models

TZ = 'America/Monterrey'
DEFAULT_OPTOUT = 'BAJA,STOP,ALTO,UNSUBSCRIBE,CANCELAR,NO ME ESCRIBAS,NO ME ESCRIBAN,NO MOLESTAR,NO MAS MENSAJES'


def _norm(text):
    t = unicodedata.normalize('NFKD', text or '')
    t = ''.join(c for c in t if not unicodedata.combining(c))
    return re.sub(r'[^A-Z0-9 ]+', ' ', t.upper()).strip()


class WhatsappPolicy(models.AbstractModel):
    _name = 'whatsapp.policy'
    _description = 'Política de envío anti-baneo'

    @api.model
    def params(self):
        P = self.env['ir.config_parameter'].sudo()

        def num(key, default):
            try:
                return int(P.get_param(key, str(default)) or default)
            except ValueError:
                return default
        return {
            'max_per_minute': max(1, num('som_whatsapp.max_per_minute', 5)),
            'jitter_min': max(0, num('som_whatsapp.jitter_min', 6)),
            'jitter_max': max(1, num('som_whatsapp.jitter_max', 14)),
            'daily_cap': max(1, num('som_whatsapp.daily_cap', 200)),
            'warmup': (P.get_param('som_whatsapp.warmup', 'True') or 'True') not in ('False', '0', ''),
            'window_start': num('som_whatsapp.window_start', 9),
            'window_end': num('som_whatsapp.window_end', 20),
            'sunday_urgent_only': (P.get_param('som_whatsapp.sunday_urgent_only', 'True') or 'True') not in ('False', '0', ''),
            'hold_spread_minutes': max(0, num('som_whatsapp.hold_spread_minutes', 150)),
            'optout_keywords': [k.strip() for k in (P.get_param('som_whatsapp.optout_keywords') or DEFAULT_OPTOUT).split(',') if k.strip()],
            'health_guard': (P.get_param('som_whatsapp.health_guard', 'True') or 'True') not in ('False', '0', ''),
        }

    @api.model
    def now_local(self):
        return pytz.utc.localize(fields.Datetime.now()).astimezone(pytz.timezone(TZ))

    @api.model
    def window_state(self):
        """('open'|'closed'|'sunday', motivo)."""
        p = self.params()
        now = self.now_local()
        if now.weekday() == 6 and p['sunday_urgent_only']:
            return 'sunday', 'domingo: solo avisos urgentes'
        if not (p['window_start'] <= now.hour < p['window_end']):
            return 'closed', 'fuera de la ventana %02d:00–%02d:00' % (p['window_start'], p['window_end'])
        return 'open', ''

    @api.model
    def daily_cap_for(self, account):
        """Rampa de calentamiento por antigüedad de la cuenta."""
        p = self.params()
        cap = p['daily_cap']
        if p['warmup'] and account and account.first_connected:
            age = (fields.Date.context_today(self) - account.first_connected).days
            if age < 7:
                cap = min(cap, 20)
            elif age < 14:
                cap = min(cap, 50)
            elif age < 30:
                cap = min(cap, 100)
        return cap

    @api.model
    def sent_today(self, account):
        start_local = self.now_local().replace(hour=0, minute=0, second=0, microsecond=0)
        start_utc = start_local.astimezone(pytz.utc).replace(tzinfo=None)
        return self.env['whatsapp.message'].sudo().search_count([
            ('direction', '=', 'out'), ('account_id', '=', account.id),
            ('state', 'in', ('sent', 'delivered', 'read')), ('sent_date', '>=', start_utc)])

    @api.model
    def is_optout_text(self, text):
        t = _norm(text)
        if not t:
            return False
        for kw in self.params()['optout_keywords']:
            k = _norm(kw)
            if k and (t == k or t.startswith(k + ' ') or (len(k) > 4 and k in t)):
                return True
        return False

    @api.model
    def vary(self, text):
        """'{Hola|Buen día} ...' → una opción al azar. No toca {{ }}."""
        return re.sub(r'\{([^{}|]*\|[^{}]*)\}', lambda m: random.choice(m.group(1).split('|')).strip(), text or '')

    @api.model
    def jitter_seconds(self):
        p = self.params()
        return random.uniform(p['jitter_min'], max(p['jitter_min'], p['jitter_max']))

    @api.model
    def spread_datetime(self, max_minutes, not_after=None):
        """Instante aleatorio entre ahora y +max_minutes (acotado por not_after − 60 min)."""
        now = fields.Datetime.now()
        limit = max_minutes
        if not_after:
            room = int((not_after - now).total_seconds() // 60) - 60
            limit = max(0, min(limit, room))
        return now + timedelta(minutes=random.uniform(0, limit)) if limit > 0 else now


class WhatsappBlocklist(models.Model):
    _name = 'whatsapp.blocklist'
    _description = 'Números que pidieron no recibir WhatsApp'
    _order = 'create_date desc'

    phone = fields.Char(required=True, index=True)
    partner_id = fields.Many2one('res.partner', string='Contacto')
    reason = fields.Char(string='Motivo')
    source_text = fields.Char(string='Texto recibido')

    _phone_uniq = models.Constraint('unique(phone)', 'Ese número ya está en la lista de baja.')

    @api.model
    def is_blocked(self, phone):
        phone = self.env['whatsapp.gateway'].normalize_phone(phone or '')
        return bool(phone) and bool(self.sudo().search_count([('phone', '=', phone)]))

    @api.model
    def block(self, phone, partner=None, reason='', source_text=''):
        phone = self.env['whatsapp.gateway'].normalize_phone(phone or '')
        if not phone:
            return self.browse()
        rec = self.sudo().search([('phone', '=', phone)], limit=1)
        if not rec:
            rec = self.sudo().create({'phone': phone, 'partner_id': partner.id if partner else False,
                                      'reason': reason, 'source_text': (source_text or '')[:200]})
        if partner:
            partner.sudo().write({'whatsapp_opt_out': True})
        return rec
