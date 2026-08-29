"""Textos de las plantillas del sistema. Cortos: de quién es, de qué trata y
con quién se da seguimiento. El aviso "número no atendido" (ctx.notice) va
SOLO en la auto-respuesta cuando el cliente escribe."""

SALE_CONFIRMED = """{Hola|Buen día|Qué tal} {{ object.partner_id.name }}, le saluda {{ object.company_id.name }}.

Su pedido *{{ object.name }}*{{ ctx.get('job_suffix') }} quedó confirmado; {adjuntamos el documento|le compartimos el documento|aquí tiene el documento}.

Para seguimiento y pagos, {{ ctx.get('followup_contact') }}"""

HOLD_CLIENT = """{Hola|Buen día|Qué tal} {{ object.partner_id.name }}, le saluda {{ object.company_id.name }}.

Su reserva *{{ object.name }}* vence *hoy a las {{ ctx.get('expiry_time') }}*.
{{ ctx.get('lines_short') }}

{Son piezas únicas|Se trata de piezas únicas}: al vencer quedan libres para otro cliente.

Para extender o confirmar, {{ ctx.get('followup_contact') }}"""

HOLD_DOCUMENT = """{Hola|Buen día|Qué tal} {{ object.partner_id.name }}, le saluda {{ object.company_id.name }}.

{Le compartimos|Le enviamos|Adjuntamos} el documento de su reserva *{{ object.name }}*.
{{ ctx.get('lines_short') }} · apartada hasta el {{ ctx.get('expiry') }}.

Son piezas únicas: después de esa fecha quedan libres para otro cliente.

Para confirmar o extender, {{ ctx.get('followup_contact') }}"""

HOLD_SELLER_T2 = """🔒 Aviso interno · Reserva *{{ object.name }}* vence {{ ctx.get('when') }} ({{ ctx.get('expiry') }})

Cliente: *{{ ctx.get('client') }}*{{ ctx.get('client_phone_pretty') and (' · ' + ctx.get('client_phone_pretty')) or '' }}
Job: {{ ctx.get('job') }}

{{ ctx.get('lines_compact') }}

Contacta al cliente para renovar o convertir en venta."""

HOLD_SELLER_T0 = """🔒 Aviso interno · Reserva *{{ object.name }}* vence *hoy a las {{ ctx.get('expiry_time') }}*

Cliente: *{{ ctx.get('client') }}*{{ ctx.get('client_phone_pretty') and (' · ' + ctx.get('client_phone_pretty')) or '' }}
Job: {{ ctx.get('job') }}

{{ ctx.get('lines_compact') }}

Al vencer, el material se libera. Contacta al cliente hoy para renovar o convertir en venta."""

INBOUND_AUTOREPLY = """{{ ctx.get('notice') }}

{{ ctx.get('seller_followup') }}"""

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
