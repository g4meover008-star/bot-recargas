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
from supabase import create_client, Client

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
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ========== FLASK APP ==========
app_flask = Flask(__name__)

@app_flask.route("/health", methods=["GET"])
def health():
    return "ok", 200

# ========== HANDLERS ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("Recargar créditos 💳", callback_data="recargar")]
    ]
    await update.message.reply_text(
        "Bienvenido 👋\nElige una opción:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "recargar":
        # Aquí se muestra el QR de Yape
        if YAPE_QR_URL:
            await query.message.reply_photo(
                photo=YAPE_QR_URL,
                caption=f"Escanea el QR para pagar.\nPrecio por crédito: {PRICE_PER_CREDIT} soles.\n\nDespués de pagar, envía captura 📷"
            )
        else:
            await query.message.reply_text("⚠️ No se configuró el QR de Yape.")

# ========== TELEGRAM BOT ==========
tg_app = ApplicationBuilder().token(TG_BOT_TOKEN).build()
tg_app.add_handler(CommandHandler("start", start))
tg_app.add_handler(CallbackQueryHandler(on_button))

# ========== MAIN ==========
if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))

    async def run_all():
        asyncio.create_task(tg_app.run_polling(close_loop=False))
        from waitress import serve
        serve(app_flask, host="0.0.0.0", port=port)

    asyncio.run(run_all())
