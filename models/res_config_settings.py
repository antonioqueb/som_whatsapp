from odoo import models, fields, _


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    wa_gateway_url = fields.Char(string='URL del gateway', config_parameter='som_whatsapp.gateway_url',
                                 help='Ej. http://som-whatsapp-gateway:3000 (red interna de Docker).')
    wa_api_key = fields.Char(string='API key', config_parameter='som_whatsapp.api_key')
    wa_webhook_token = fields.Char(string='Token de webhook', config_parameter='som_whatsapp.webhook_token',
                                   help='Debe coincidir con WEBHOOK_TOKEN del gateway.')
    wa_default_country_code = fields.Char(string='Código de país por defecto', config_parameter='som_whatsapp.default_country_code', default='52')
    wa_timeout = fields.Char(string='Timeout (s)', config_parameter='som_whatsapp.timeout', default='20')
    # Char (no Text): res.config.settings solo admite boolean/integer/float/
    # char/selection/many2one/datetime en campos con config_parameter; con
    # Text el default_get de Ajustes truena y la pantalla completa no abre.
    wa_notice_text = fields.Char(
        string='Aviso "solo notificaciones"', config_parameter='som_whatsapp.notice_text',
        help='Texto que va en todo mensaje al cliente: este número no se atiende, el seguimiento es con su asesor. '
             'Vacío = texto por defecto.')
    wa_autoreply_cooldown_hours = fields.Integer(
        string='Enfriamiento de auto-respuesta (h)', config_parameter='som_whatsapp.autoreply_cooldown_hours', default=12,
        help='Al cliente que escribe a este número se le responde una vez por esta ventana; su mensaje SIEMPRE se reenvía al asesor.')
    wa_fallback_forward_phone = fields.Char(
        string='Número de respaldo (sin asesor)', config_parameter='som_whatsapp.fallback_forward_phone',
        help='Si un cliente escribe y no se identifica asesor, el mensaje se reenvía aquí (p. ej. gerencia de ventas).')
    wa_seller_accounts = fields.Boolean(string='Teléfonos de vendedores', config_parameter='som_whatsapp.seller_accounts', default=True,
                                        help='Cada vendedor puede vincular su teléfono (WhatsApp › Mi WhatsApp). Lo suyo sale desde su número; si no está conectado, sale del genérico.')
    wa_max_per_minute = fields.Integer(string='Máximo por minuto', config_parameter='som_whatsapp.max_per_minute', default=5)
    wa_jitter_min = fields.Integer(string='Pausa mínima entre mensajes (s)', config_parameter='som_whatsapp.jitter_min', default=6)
    wa_jitter_max = fields.Integer(string='Pausa máxima entre mensajes (s)', config_parameter='som_whatsapp.jitter_max', default=14)
    wa_daily_cap = fields.Integer(string='Tope diario por cuenta', config_parameter='som_whatsapp.daily_cap', default=200)
    wa_warmup = fields.Boolean(string='Rampa de calentamiento (20 / 50 / 100 por día las primeras semanas)', config_parameter='som_whatsapp.warmup', default=True)
    wa_window_start = fields.Integer(string='Ventana de envío: desde (h)', config_parameter='som_whatsapp.window_start', default=9)
    wa_window_end = fields.Integer(string='Ventana de envío: hasta (h)', config_parameter='som_whatsapp.window_end', default=20)
    wa_sunday_urgent_only = fields.Boolean(string='Domingo: solo avisos urgentes', config_parameter='som_whatsapp.sunday_urgent_only', default=True)
    wa_hold_spread_minutes = fields.Integer(string='Escalonar avisos de reservas (min)', config_parameter='som_whatsapp.hold_spread_minutes', default=150)
    wa_optout_keywords = fields.Char(string='Palabras de baja', config_parameter='som_whatsapp.optout_keywords',
                                     help='Separadas por coma. Quien las escriba deja de recibir mensajes al instante.')
    wa_health_guard = fields.Boolean(string='Detector de bloqueos (pausa la cuenta sola)', config_parameter='som_whatsapp.health_guard', default=True)

    # ── Asistente IA (DeepSeek / API compatible con OpenAI) ──
    wa_ai_enabled = fields.Boolean(string='Asistente IA por WhatsApp', config_parameter='som_whatsapp.ai_enabled',
                                   help='Solo atiende a los números de la lista blanca (WhatsApp › Asistente IA › Números autorizados).')
    wa_ai_api_url = fields.Char(string='URL de la API', config_parameter='som_whatsapp.ai_api_url', default='https://api.deepseek.com')
    wa_ai_api_key = fields.Char(string='API key', config_parameter='som_whatsapp.ai_api_key')
    wa_ai_model = fields.Char(string='Modelo', config_parameter='som_whatsapp.ai_model', default='deepseek-chat')
    wa_ai_temperature = fields.Float(string='Temperatura', config_parameter='som_whatsapp.ai_temperature', default=0.2)
    wa_ai_max_tool_calls = fields.Integer(string='Consultas a Odoo por mensaje (máx.)', config_parameter='som_whatsapp.ai_max_tool_calls', default=6)
    wa_ai_context_turns = fields.Integer(string='Turnos de contexto', config_parameter='som_whatsapp.ai_context_turns', default=12)
    wa_ai_timeout = fields.Integer(string='Timeout IA (s)', config_parameter='som_whatsapp.ai_timeout', default=60)
    wa_ai_system_prompt = fields.Char(string='Instrucciones adicionales (todas las conversaciones)', config_parameter='som_whatsapp.ai_system_prompt')
    wa_ai_stt_url = fields.Char(string='Transcripción de audio: URL', config_parameter='som_whatsapp.ai_stt_url',
                                help='API compatible con OpenAI (/audio/transcriptions). Ej. https://api.openai.com/v1 o https://api.groq.com/openai/v1. DeepSeek no transcribe audio.')
    wa_ai_stt_key = fields.Char(string='Transcripción de audio: API key', config_parameter='som_whatsapp.ai_stt_key')
    wa_ai_stt_model = fields.Char(string='Transcripción de audio: modelo', config_parameter='som_whatsapp.ai_stt_model', default='whisper-1',
                                  help='OpenAI: whisper-1 · Groq: whisper-large-v3-turbo')
    wa_hold_morning_hour = fields.Integer(
        string='Hora de envío (reservas)', config_parameter='som_whatsapp_holds.morning_hour', default=9,
        help='Hora local (Monterrey) a partir de la cual el cron manda los avisos de reservas. '
             'Vendedor: 2 días antes y el día del vencimiento. Cliente: la mañana del vencimiento '
             '(o la del día anterior si vence temprano).')
    def action_wa_test(self):
        h = self.env['whatsapp.gateway'].health()
        return {
            'type': 'ir.actions.client', 'tag': 'display_notification',
            'params': {'title': _('Gateway WhatsApp'), 'type': 'success' if h.get('ok') else 'danger', 'sticky': False,
                       'message': _('Conectado · sesiones: %s') % (', '.join(h.get('sessions') or []) or _('ninguna'))},
        }
