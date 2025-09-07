import os, io, uuid, asyncio, logging, qrcode
from datetime import datetime
from flask import Flask, request, jsonify
from supabase import create_client

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Bot
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
)

# ========= LOGGING =========
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("recargas")

# ========= ENV =========
TG_BOT_TOKEN = os.getenv("TG_RECHARGE_BOT_TOKEN", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_ANON_KEY") or os.getenv("SUPABASE_API_KEY") or ""
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://localhost:8080")
CURRENCY = os.getenv("CURRENCY", "PEN")
PRICE_PER_CREDIT = float(os.getenv("PRICE_PER_CREDIT", "1"))

if not (TG_BOT_TOKEN and SUPABASE_URL and SUPABASE_KEY and PUBLIC_BASE_URL):
    raise SystemExit("❌ Faltan variables de entorno necesarias.")

# ========= CLIENTES =========
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
app_flask = Flask(__name__)

tg_app = ApplicationBuilder().token(TG_BOT_TOKEN).build()
bot = Bot(TG_BOT_TOKEN, parse_mode="HTML")

# ========= DB helpers =========
def pagos_upsert(pago_id: str, user_id: str, username: str, amount: float):
    supabase.table("pagos").upsert({
        "id": pago_id,
        "user_id": str(user_id),
        "username": username,
        "amount": amount,
        "status": "pendiente",
        "created_at": datetime.utcnow().isoformat()
    }, on_conflict="id").execute()

def pagos_set_status(pago_id: str, new_status: str):
    supabase.table("pagos").update({
        "status": new_status,
        "updated_at": datetime.utcnow().isoformat()
    }).eq("id", pago_id).execute()

def pagos_get(pago_id: str):
    r = supabase.table("pagos").select("*").eq("id", pago_id).limit(1).execute()
    return r.data[0] if r.data else None

def user_add_credits(user_id: str, amount_paid: float):
    r = supabase.table("usuarios").select("creditos").eq("telegram_id", str(user_id)).limit(1).execute()
    current = int(r.data[0]["creditos"]) if r.data and r.data[0].get("creditos") else 0
    to_add = int(round(amount_paid / PRICE_PER_CREDIT))
    new_value = current + to_add

    supabase.table("usuarios").update({"creditos": new_value}).eq("telegram_id", str(user_id)).execute()
    return to_add, new_value

# ========= QR helper =========
def build_qr_png_bytes(url: str) -> bytes:
    img = qrcode.make(url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()

# ========= TELEGRAM HANDLERS =========
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 Bot de Recargas\n"
        "Usa /recargar <monto>\n\n"
        "Ejemplo: /recargar 5"
    )

async def cmd_recargar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if len(context.args) != 1:
        await update.message.reply_text("Uso: /recargar <monto>")
        return

    try:
        amount = float(context.args[0])
        if amount <= 0:
            raise ValueError()
    except Exception:
        await update.message.reply_text("Monto inválido. Ej: /recargar 5")
        return

    pago_id = str(uuid.uuid4())
    pagos_upsert(pago_id, str(user.id), user.username or "", amount)

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📷 Enviar captura del pago", callback_data=f"pago:{pago_id}")]
    ])
    await update.message.reply_text(
        f"💳 Solicitud creada por {amount:.2f} {CURRENCY}.\n"
        f"ID de pedido: <code>{pago_id}</code>\n\n"
        "Envía la captura de tu pago para que el admin lo confirme.",
        reply_markup=kb
    )

async def on_pago_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    if not data.startswith("pago:"):
        return
    pago_id = data.split(":", 1)[1]

    row = pagos_get(pago_id)
    if not row:
        await q.message.reply_text("No encuentro ese pedido.")
        return

    await q.message.reply_text("📷 Envía tu captura del pago en este chat.")

# ========= FLASK ROUTES =========
@app_flask.route("/health", methods=["GET"])
def health():
    return "ok", 200

# ========= MAIN =========
if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))

    tg_app.add_handler(CommandHandler("start", cmd_start))
    tg_app.add_handler(CommandHandler("recargar", cmd_recargar))
    tg_app.add_handler(CallbackQueryHandler(on_pago_callback, pattern=r"^pago:"))

    async def run_all():
        asyncio.create_task(tg_app.run_polling(close_loop=False))
        from waitress import serve
        serve(app_flask, host="0.0.0.0", port=port)

    asyncio.run(run_all())
