import os, io, uuid, logging, qrcode
from datetime import datetime
from flask import Flask, request
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, CallbackQueryHandler
from supabase import create_client, Client
import asyncio
from waitress import serve

# ========= LOGGING =========
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("recargas")

# ========= ENV =========
TG_BOT_TOKEN = os.getenv("TG_RECHARGE_BOT_TOKEN", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_ANON_KEY") or os.getenv("SUPABASE_API_KEY") or ""
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "")
CURRENCY = os.getenv("CURRENCY", "PEN")
PRICE_PER_CREDIT = float(os.getenv("PRICE_PER_CREDIT", "1"))
YAPE_NUMBER = os.getenv("YAPE_NUMBER", "999999999")  # Tu número Yape

if not (TG_BOT_TOKEN and SUPABASE_URL and SUPABASE_KEY and PUBLIC_BASE_URL and YAPE_NUMBER):
    raise SystemExit("Faltan variables de entorno necesarias")

# ========= CLIENTES =========
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
app = Flask(__name__)
bot = Bot(TG_BOT_TOKEN, parse_mode="HTML")
tg_app = ApplicationBuilder().token(TG_BOT_TOKEN).build()

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
    data = {"status": new_status, "updated_at": datetime.utcnow().isoformat()}
    supabase.table("pagos").update(data).eq("id", pago_id).execute()

def user_add_credits(user_id: str, amount_paid: float):
    r = supabase.table("usuarios").select("creditos").eq("telegram_id", str(user_id)).limit(1).execute()
    current = int(r.data[0]["creditos"]) if r.data and r.data[0].get("creditos") is not None else 0
    to_add = int(round(amount_paid / PRICE_PER_CREDIT))
    new_value = current + to_add
    supabase.table("usuarios").update({"creditos": new_value}).eq("telegram_id", str(user_id)).execute()
    supabase.table("creditos_historial").insert({
        "usuario_id": str(user_id),
        "delta": to_add,
        "motivo": "recarga_yape",
        "hecho_por": "yape"
    }).execute()
    return to_add, new_value

# ========= QR =========
def build_yape_qr(amount: float, pago_id: str) -> bytes:
    url = f"yape://pay?number={YAPE_NUMBER}&amount={amount}&id={pago_id}"
    img = qrcode.make(url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()

# ========= TELEGRAM =========
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 Bot de Recargas con Yape\n"
        "Usa /recargar <monto>\n\n"
        "Ejemplo:\n/recargar 5"
    )

async def cmd_recargar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if len(context.args) != 1:
        await update.message.reply_text("Uso: /recargar <monto>. Ej: /recargar 5")
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
        [InlineKeyboardButton("📲 Ver QR Yape", callback_data=f"qr:{pago_id}:{amount}")]
    ])
    await update.message.reply_text(
        f"💵 Recarga solicitada: {amount:.2f} {CURRENCY}\n"
        f"ID de pedido: <code>{pago_id}</code>\n\n"
        "Escanea el QR y paga con Yape.\n"
        "Cuando confirmemos el pago, tus créditos se acreditarán.",
        reply_markup=kb
    )

async def on_qr_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data.split(":")
    if len(data) != 3:
        return
    pago_id, amount = data[1], float(data[2])

    png = build_yape_qr(amount, pago_id)
    await q.message.reply_photo(png, caption="Escanea este QR para pagar con Yape 📲")

tg_app.add_handler(CommandHandler("start", cmd_start))
tg_app.add_handler(CommandHandler("recargar", cmd_recargar))
tg_app.add_handler(CallbackQueryHandler(on_qr_callback, pattern=r"^qr:"))

# ========= FLASK =========
@app.route("/health", methods=["GET"])
def health():
    return "ok", 200

# ========= MAIN =========
if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))

    async def run_all():
        task_bot = asyncio.create_task(tg_app.run_polling(close_loop=False))

        loop = asyncio.get_event_loop()
        def run_flask():
            serve(app, host="0.0.0.0", port=port)
        task_flask = loop.run_in_executor(None, run_flask)

        await asyncio.gather(task_bot, task_flask)

    asyncio.run(run_all())
