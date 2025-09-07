import os, io, uuid, asyncio, logging
from datetime import datetime
from flask import Flask, request

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Bot
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, CallbackQueryHandler

from supabase import create_client
import qrcode

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("recargas")

# ========= ENV =========
TG_BOT_TOKEN = os.getenv("TG_RECHARGE_BOT_TOKEN", "")
TG_ADMIN_BOT_TOKEN = os.getenv("BOT_TOKEN", "")   # Bot principal (admin)
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_ANON_KEY") or os.getenv("SUPABASE_API_KEY") or ""
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "")
PRICE_PER_CREDIT = float(os.getenv("PRICE_PER_CREDIT", "1"))
YAPE_QR_PATH = os.getenv("YAPE_QR_PATH", "yape_qr.png")  # Ruta QR fijo

if not (TG_BOT_TOKEN and TG_ADMIN_BOT_TOKEN and SUPABASE_URL and SUPABASE_KEY):
    raise SystemExit("Faltan variables de entorno necesarias")

# ========= CLIENTES =========
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
app_flask = Flask(__name__)

# Bots
tg_app = ApplicationBuilder().token(TG_BOT_TOKEN).build()
admin_bot = Bot(TG_ADMIN_BOT_TOKEN, parse_mode="HTML")

# ========= HELPERS =========
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

def user_add_credits(user_id: str, amount_paid: float):
    r = supabase.table("usuarios").select("creditos").eq("telegram_id", str(user_id)).limit(1).execute()
    current = int(r.data[0]["creditos"]) if r.data and r.data[0].get("creditos") is not None else 0
    to_add = int(round(amount_paid / PRICE_PER_CREDIT))
    new_value = current + to_add
    supabase.table("usuarios").update({"creditos": new_value}).eq("telegram_id", str(user_id)).execute()
    return to_add, new_value

# ========= QR =========
def get_yape_qr_bytes() -> bytes:
    if os.path.exists(YAPE_QR_PATH):
        with open(YAPE_QR_PATH, "rb") as f:
            return f.read()
    else:
        img = qrcode.make("YAPE")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

# ========= TELEGRAM HANDLERS =========
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 Bot de Recargas con Yape\n"
        "Usa /recargar <monto>\n\n"
        "Ejemplo:\n"
        "/recargar 5\n\n"
        "Recibirás el QR de Yape y deberás enviar la captura del pago."
    )

async def cmd_recargar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if len(context.args) != 1:
        await update.message.reply_text("Uso: /recargar <monto>\nEj: /recargar 5")
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
        [InlineKeyboardButton("📤 Enviar captura del pago", callback_data=f"pago:{pago_id}")]
    ])

    # QR Yape
    qr_bytes = get_yape_qr_bytes()
    await update.message.reply_photo(qr_bytes, caption=(
        f"💳 Paga {amount:.2f} soles con Yape usando este QR.\n\n"
        "Luego, sube la captura aquí para validar tu recarga."
    ), reply_markup=kb)

async def on_pago_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not q.data.startswith("pago:"):
        return
    pago_id = q.data.split(":", 1)[1]
    await q.message.reply_text("📤 Por favor, envíame la captura del pago como imagen.")

async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not update.message.photo:
        return
    pago_id = str(uuid.uuid4())
    file_id = update.message.photo[-1].file_id
    pagos_upsert(pago_id, str(user.id), user.username or "", 0)

    # Notificación al admin
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Aprobar", callback_data=f"approve:{pago_id}:{user.id}")],
        [InlineKeyboardButton("❌ Rechazar", callback_data=f"reject:{pago_id}:{user.id}")]
    ])
    await admin_bot.send_photo(
        chat_id=os.getenv("ADMIN_CHAT_ID"),
        photo=file_id,
        caption=f"Solicitud de recarga\nUsuario: @{user.username}\nID: {user.id}\nPago ID: {pago_id}",
        reply_markup=kb
    )
    await update.message.reply_text("✅ Tu comprobante fue enviado al administrador, espera confirmación.")

async def on_admin_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data.split(":")
    action, pago_id, user_id = data[0], data[1], int(data[2])

    if action == "approve":
        added, total = user_add_credits(user_id, 5)  # aquí fijas monto
        pagos_set_status(pago_id, "aprobado")
        await tg_app.bot.send_message(chat_id=user_id, text=f"✅ Recarga aprobada. Créditos añadidos: {added}. Total: {total}")
        await q.message.reply_text("✔️ Recarga aprobada.")
    elif action == "reject":
        pagos_set_status(pago_id, "rechazado")
        await tg_app.bot.send_message(chat_id=user_id, text="❌ Tu recarga fue rechazada.")
        await q.message.reply_text("❌ Recarga rechazada.")

# ========= HANDLERS =========
tg_app.add_handler(CommandHandler("start", cmd_start))
tg_app.add_handler(CommandHandler("recargar", cmd_recargar))
tg_app.add_handler(CallbackQueryHandler(on_pago_callback, pattern=r"^pago:"))
tg_app.add_handler(CallbackQueryHandler(on_admin_action, pattern=r"^(approve|reject):"))
tg_app.add_handler(CommandHandler("photo", on_photo))
tg_app.add_handler(CommandHandler("image", on_photo))

# ========= FLASK =========
@app_flask.route("/health", methods=["GET"])
def health():
    return "ok", 200

# ========= MAIN =========
if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))

    async def run_all():
        asyncio.create_task(tg_app.run_polling(close_loop=False))
        from waitress import serve
        serve(app_flask, host="0.0.0.0", port=port)

    asyncio.run(run_all())
