"""Textos de las plantillas del sistema. Cortos: de quién es, de qué trata y
con quién se da seguimiento. El aviso "número no atendido" (ctx.notice) va
SOLO en la auto-respuesta cuando el cliente escribe."""

SALE_CONFIRMED = """Hola {{ object.partner_id.name }}, le saluda {{ object.company_id.name }}.
Su pedido *{{ object.name }}*{{ ctx.get('job_suffix') }} quedó confirmado; adjuntamos el documento.
{{ ctx.get('seller_block') }}"""

HOLD_CLIENT = """Hola {{ object.partner_id.name }}, le saluda {{ object.company_id.name }}.
Su reserva *{{ object.name }}* ({{ ctx.get('lines_short') }}) vence {{ ctx.get('when') }} a las {{ ctx.get('expiry_time') }}. Son piezas únicas: al vencer quedan libres para otro cliente y no podemos reponerlas.
Para extenderla o confirmar su pedido, su asesor {{ ctx.get('seller_contact') }}."""

HOLD_DOCUMENT = """Hola {{ object.partner_id.name }}, le saluda {{ object.company_id.name }}.
Le compartimos el documento de su reserva *{{ object.name }}* ({{ ctx.get('lines_short') }}), apartada solo hasta el {{ ctx.get('expiry') }} hora de Monterrey. Son piezas únicas: después de esa fecha quedan libres para otro cliente.
Para confirmar o extender, su asesor {{ ctx.get('seller_contact') }}."""

HOLD_SELLER_T2 = """⏳ *Reserva {{ object.name }} vence {{ ctx.get('when') }}* · {{ ctx.get('expiry') }}
Cliente: {{ ctx.get('client') }} · Job: {{ ctx.get('job') }}
{{ ctx.get('lines') }}
Al vencer, otro vendedor puede tomarlas. 🔗 {{ ctx.get('link') }}"""

HOLD_SELLER_T0 = """⚠️ *Reserva {{ object.name }} vence HOY a las {{ ctx.get('expiry_time') }}*
Cliente: {{ ctx.get('client') }} · Job: {{ ctx.get('job') }}
{{ ctx.get('lines') }}
Renuévala o conviértela en venta; al vencer, el material se libera.
🔗 {{ ctx.get('link') }}"""

INBOUND_AUTOREPLY = """{{ ctx.get('notice') }} Su mensaje fue turnado a su asesor.
{{ ctx.get('seller_block') }}"""

INBOUND_FORWARD = """📨 El cliente *{{ ctx.get('client') or 'Desconocido' }}*{{ ctx.get('ref') and (' sobre ' + ctx.get('ref')) or '' }} envió:
{{ ctx.get('client_text_line') }}
Dale seguimiento. Su número: {{ ctx.get('client_phone_pretty') }}"""

BY_XMLID = {
    'som_whatsapp.wa_template_sale_confirmed': SALE_CONFIRMED,
    'som_whatsapp.wa_template_hold_client': HOLD_CLIENT,
    'som_whatsapp.wa_template_hold_document': HOLD_DOCUMENT,
    'som_whatsapp.wa_template_hold_seller_t2': HOLD_SELLER_T2,
    'som_whatsapp.wa_template_hold_seller_t0': HOLD_SELLER_T0,
    'som_whatsapp.wa_template_inbound_autoreply': INBOUND_AUTOREPLY,
    'som_whatsapp.wa_template_inbound_forward': INBOUND_FORWARD,
}
