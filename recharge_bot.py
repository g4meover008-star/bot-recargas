import os, io, uuid, asyncio, logging, qrcode
from datetime import datetime
from flask import Flask, request, jsonify
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, CallbackQueryHandler
from supabase import create_client, Client

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("recargas")

# ========= ENV =========
TG_BOT_TOKEN = os.getenv("TG_RECHARGE_BOT_TOKEN", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_ANON_KEY") or os.getenv("SUPABASE_API_KEY") or ""
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "")
PRICE_PER_CREDIT = float(os.getenv("PRICE_PER_CREDIT", "1"))

# ========= DEBUG: imprimir qué variables llegan =========
def mask(val: str, keep: int = 6):
    """Oculta valores largos para debug seguro"""
    if not val:
        return None
    return val[:keep] + "..." if len(val) > keep else val

print("===== VARIABLES DE ENTORNO DETECTADAS =====")
print("TG_RECHARGE_BOT_TOKEN:", mask(TG_BOT_TOKEN))
print("SUPABASE_URL:", SUPABASE_URL)
print("SUPABASE_KEY:", mask(SUPABASE_KEY))
print("PUBLIC_BASE_URL:", PUBLIC_BASE_URL)
print("PRICE_PER_CREDIT:", PRICE_PER_CREDIT)
print("===========================================")

# ========= VALIDACIÓN =========
if not (TG_BOT_TOKEN and SUPABASE_URL and SUPABASE_KEY and PUBLIC_BASE_URL):
    raise SystemExit("❌ ERROR: Faltan variables de entorno necesarias -> "
                     "TG_RECHARGE_BOT_TOKEN, SUPABASE_URL, SUPABASE_ANON_KEY/API_KEY, PUBLIC_BASE_URL")

# ========= CLIENTES =========
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
app_flask = Flask(__name__)
tg_app = ApplicationBuilder().token(TG_BOT_TOKEN).build()

# ========= HANDLERS DE EJEMPLO =========
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 Bot de Recargas listo.\n"
        "Usa /recargar <monto> para iniciar una solicitud."
    )

tg_app.add_handler(CommandHandler("start", cmd_start))

@app_flask.route("/health", methods=["GET"])
def health():
    return "ok", 200

# ========= MAIN =========
if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))

    async def run_all():
        # arranca el bot de TG en segundo plano
        asyncio.create_task(tg_app.run_polling(close_loop=False))
        # arranca Flask (servidor http)
        from waitress import serve
        serve(app_flask, host="0.0.0.0", port=port)

    asyncio.run(run_all())
