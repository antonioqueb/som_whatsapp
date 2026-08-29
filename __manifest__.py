{
    'name': 'SOM WhatsApp (Baileys)',
    'version': '19.0.1.9.0',
    'category': 'Tools',
    'summary': 'Notificaciones WhatsApp vía gateway Baileys con API HTTP estándar; puntos de conexión para enlazar procesos',
    'description': """
WhatsApp para Odoo (SOM)
========================
- Gateway Node/Baileys (carpeta gateway/) con API HTTP estándar y webhooks.
- Cuentas WhatsApp emparejadas por QR desde Odoo.
- Bitácora de mensajes (salida/entrada) con estados enviado/entregado/leído.
- Plantillas renderizables (inline template: {{ object.name }}).
- Puntos de conexión: evento por modelo → plantilla → destinatario, para
  enganchar procesos del sistema en iteraciones posteriores con una línea:
      self.env['whatsapp.event'].fire('sale.order.confirmed', orders)
- Compositor "Enviar WhatsApp" desde cualquier registro.
    """,
    'author': 'Alphaqueb Consulting',
    'website': 'https://alphaqueb.com',
    'license': 'LGPL-3',
    'depends': ['base', 'mail', 'web', 'sale', 'stock_lot_dimensions'],
    'data': [
        'security/security.xml',
        'security/ir.model.access.csv',
        'data/ir_cron.xml',
        'views/whatsapp_account_views.xml',
        'views/whatsapp_message_views.xml',
        'views/whatsapp_template_views.xml',
        'views/whatsapp_event_views.xml',
        'views/res_config_settings_views.xml',
        'views/res_partner_views.xml',
        'wizard/whatsapp_compose_views.xml',
        'data/whatsapp_hold_data.xml',
        'data/whatsapp_sale_data.xml',
        'data/whatsapp_inbound_data.xml',
        'views/sale_order_views.xml',
        'views/stock_lot_hold_order_views.xml',
        'views/menus.xml',
    ],
    'installable': True,
    'application': True,
}
