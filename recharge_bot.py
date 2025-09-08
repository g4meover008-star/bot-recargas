import os, asyncio, logging
from datetime import datetime
from flask import Flask, request
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
)
from supabase import create_client

# ========== LOGGING ==========
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("recargas")

# ========== VARIABLES DE ENTORNO ==========
TG_BOT_TOKEN = os.getenv("TG_RECHARGE_BOT_TOKEN", "")
ADMIN_BOT_TOKEN = os.getenv("ADMIN_BOT_TOKEN", "")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_API_KEY", "")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "")
PRICE_PER_CREDIT = float(os.getenv("PRICE_PER_CREDIT", "1"))
YAPE_QR_URL = os.getenv("YAPE_QR_URL", "")

# ========== SUPABASE CLIENT ==========
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# ========== FLASK SERVER ==========
app_flask = Flask(__name__)

@app_flask.route("/health", methods=["GET"])
def health():
    return "ok", 200

# ========== COMANDOS DEL BOT ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Hola! Bienvenido al bot de recargas.\n"
        "Usa /recargar para iniciar tu recarga."
    )

async def recargar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("📸 Enviar captura de pago", callback_data="enviar_pago")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        f"💳 Para recargar, paga con Yape.\n"
        f"👉 Precio por crédito: {PRICE_PER_CREDIT}.\n\n"
        f"📷 Escanea este QR y envía tu captura: {YAPE_QR_URL}",
        reply_markup=reply_markup
    )

async def on_payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "enviar_pago":
        await query.edit_message_text(
            "📸 Envía aquí la captura de tu pago.\n"
            "Un administrador validará tu recarga."
        )

# ========== MAIN ==========
if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))

    async def run_all():
        tg_app = (
            ApplicationBuilder()
            .token(TG_BOT_TOKEN)
            .build()
        )

        tg_app.add_handler(CommandHandler("start", start))
        tg_app.add_handler(CommandHandler("recargar", recargar))
        tg_app.add_handler(CallbackQueryHandler(on_payment_callback))

        # Ejecutar bot y servidor Flask
        asyncio.create_task(tg_app.run_polling(close_loop=False))

        from waitress import serve
        serve(app_flask, host="0.0.0.0", port=port)

    asyncio.run(run_all())
