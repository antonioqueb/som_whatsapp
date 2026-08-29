import logging
from datetime import datetime

import pytz

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

TZ = 'America/Monterrey'
MESES = ['ene', 'feb', 'mar', 'abr', 'may', 'jun', 'jul', 'ago', 'sep', 'oct', 'nov', 'dic']

KEY_SELLER_T2 = 'hold.seller_t2'
KEY_SELLER_T0 = 'hold.seller_t0'
KEY_CLIENT = 'hold.client_expiry'


class StockLotHoldOrder(models.Model):
    _inherit = 'stock.lot.hold.order'

    # Banderas por etapa: se re-arman solas cuando cambia la fecha de
    # vencimiento (renovar), igual que los avisos por correo del core.
    x_wa_seller_t2_sent = fields.Boolean(string='WA vendedor T-2', copy=False)
    x_wa_seller_t0_sent = fields.Boolean(string='WA vendedor día', copy=False)
    x_wa_client_sent = fields.Boolean(string='WA cliente', copy=False)

    def write(self, vals):
        if 'fecha_expiracion' in vals:
            vals = dict(vals, x_wa_seller_t2_sent=False, x_wa_seller_t0_sent=False, x_wa_client_sent=False)
        return super().write(vals)

    # ── helpers de contenido ──
    def _wa_local_expiry(self):
        self.ensure_one()
        if not self.fecha_expiracion:
            return None
        return pytz.utc.localize(self.fecha_expiracion).astimezone(pytz.timezone(TZ))

    @staticmethod
    def _wa_fmt(dt, with_time=True):
        if not dt:
            return ''
        s = '%d %s %d' % (dt.day, MESES[dt.month - 1], dt.year)
        if with_time:
            s += ' %s' % dt.strftime('%H:%M')
        return s

    def _wa_link(self):
        self.ensure_one()
        base = (self.get_base_url() or '').rstrip('/')
        if base.startswith('http://') and 'localhost' not in base and '127.0.0.1' not in base:
            base = 'https://' + base[7:]
        return '%s/odoo/stock.lot.hold.order/%d' % (base, self.id)

    def _wa_lines_text(self):
        """Resumen en texto plano del material (WhatsApp no lleva HTML)."""
        self.ensure_one()
        rows, placas, m2 = [], 0, 0.0
        for line in self.hold_line_ids:
            if line.product_id.type == 'service':
                continue
            lots = line.lot_ids
            names = [l.name for l in lots[:6] if l.name]
            extra = len(lots) - len(names)
            lot_txt = ', '.join(names) + (' (+%d más)' % extra if extra > 0 else '')
            rows.append('• %s — %d placa(s) · %.2f m²%s' % (
                line.x_mask_name or line.product_id.display_name, len(lots),
                line.cantidad_m2 or 0.0, ('\n   %s' % lot_txt) if lot_txt else ''))
            placas += len(lots)
            m2 += line.cantidad_m2 or 0.0
        if not rows:
            return ''
        return '\n'.join(rows) + '\n*Total: %d placa(s) · %.2f m²*' % (placas, m2)

    def _wa_lines_short(self):
        """Una línea: '3 productos · 22 placas · 105.14 m²'."""
        self.ensure_one()
        lines = self.hold_line_ids.filtered(lambda l: l.product_id.type != 'service')
        placas = sum(len(l.lot_ids) for l in lines)
        m2 = sum((l.cantidad_m2 or 0.0) for l in lines)
        if not lines:
            return ''
        return '%d producto(s) · %d placa(s) · %.2f m²' % (len(lines), placas, m2)

    def _wa_ctx(self, when_text=''):
        self.ensure_one()
        exp = self._wa_local_expiry()
        seller = self.user_id
        seller_phone = seller.partner_id.phone or ''
        job = self.project_id.name or ''
        return {
            'when': when_text,
            'job': job or '—',
            'job_suffix': (' del proyecto *%s*' % job) if job else '',
            'client': self.partner_id.display_name or '',
            'seller': seller.name or '',
            'seller_phone_suffix': (' o al %s' % seller_phone) if seller_phone else '',
            'expiry': self._wa_fmt(exp),
            'expiry_day': self._wa_fmt(exp, with_time=False),
            'expiry_time': exp.strftime('%H:%M') if exp else '',
            'lines': self._wa_lines_text(),
            'lines_short': self._wa_lines_short(),
            'link': self._wa_link(),
        }

    # ── disparo ──
    def _wa_fire_stage(self, key, flag, phone, who, when_text=''):
        """Dispara el punto de conexión `key` y marca `flag`. Sin teléfono
        no se intenta nada (solo se deja rastro en el chatter)."""
        self.ensure_one()
        if not phone:
            self.with_context(mail_create_nolog=True).write({flag: True})
            self.message_post(body='WhatsApp %s omitido: %s no tiene teléfono registrado.' % (key, who))
            return self.env['whatsapp.message']
        msgs = self.env['whatsapp.event'].sudo().fire(key, self, extra_ctx=self._wa_ctx(when_text))
        self.write({flag: True})
        if msgs:
            self.message_post(body='WhatsApp enviado a %s (%s%s).' % (
                who, key, (' · vence %s' % when_text) if when_text else ''))
        else:
            self.message_post(body='WhatsApp %s NO encolado para %s (sin punto de conexión activo, '
                                   'opt-out o error del gateway; ver bitácora WhatsApp).' % (key, who))
        return msgs

    @api.model
    def _cron_wa_hold_notices(self):
        """Cada hora. Solo actúa a partir de la hora matutina configurada.
        Vendedor: T-2 (vence en 2 días) y T-0 (vence hoy).
        Cliente: la mañana del día que vence; si vence demasiado temprano,
        la mañana del día anterior. Solo si el cliente tiene teléfono."""
        P = self.env['ir.config_parameter'].sudo()
        try:
            morning = int(P.get_param('som_whatsapp_holds.morning_hour', '9') or 9)
        except ValueError:
            morning = 9
        try:
            min_hours = int(P.get_param('som_whatsapp_holds.client_min_hours', '3') or 3)
        except ValueError:
            min_hours = 3
        now_utc = fields.Datetime.now()
        now_local = pytz.utc.localize(now_utc).astimezone(pytz.timezone(TZ))
        if now_local.hour < morning:
            return
        today = now_local.date()
        orders = self.sudo().search([
            ('state', '=', 'confirmed'),
            ('fecha_expiracion', '>', now_utc),
        ])
        for order in orders:
            try:
                exp = order._wa_local_expiry()
                days = (exp.date() - today).days
                seller_phone = order.user_id.partner_id.phone or ''
                seller_who = 'el vendedor %s' % (order.user_id.name or '')
                # Vendedor T-2 (si el cron no corrió ese día, alcanza en T-1)
                if not order.x_wa_seller_t2_sent and days in (1, 2):
                    order._wa_fire_stage(KEY_SELLER_T2, 'x_wa_seller_t2_sent', seller_phone, seller_who,
                                         'en 2 días' if days == 2 else 'mañana')
                # Vendedor día del vencimiento
                if not order.x_wa_seller_t0_sent and days == 0:
                    order._wa_fire_stage(KEY_SELLER_T0, 'x_wa_seller_t0_sent', seller_phone, seller_who, 'HOY')
                # Cliente: mañana del vencimiento, o la anterior si vence temprano
                if not order.x_wa_client_sent:
                    early = exp.hour < (morning + min_hours)
                    if days == 0 or (days == 1 and early):
                        order._wa_fire_stage(KEY_CLIENT, 'x_wa_client_sent', order.partner_id.phone or '',
                                             'el cliente %s' % (order.partner_id.display_name or ''),
                                             'hoy' if days == 0 else 'mañana')
                self.env.cr.commit()  # cada reserva cierra su propia transacción
            except Exception:  # noqa: BLE001
                _logger.exception('[WA HOLD] aviso fallido para %s', order.name)
                self.env.cr.rollback()

    def action_wa_open_compose(self):
        """Botón: enviar al cliente el documento de la reserva (resumen / detalle / sin precios)."""
        self.ensure_one()
        return self.env['whatsapp.compose'].open_for(self, 'som_whatsapp.wa_template_hold_document',
                                                     'stock_lot_dimensions.action_report_stock_lot_hold_order_summary')

    def action_wa_send_seller_now(self):
        """Botón: manda ahora mismo el aviso del día al vendedor (prueba/forzado)."""
        for order in self:
            order._wa_fire_stage(KEY_SELLER_T0, 'x_wa_seller_t0_sent', order.user_id.partner_id.phone or '',
                                 'el vendedor %s' % (order.user_id.name or ''),
                                 self._wa_fmt(order._wa_local_expiry(), with_time=False))
