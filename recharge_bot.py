import os, io, uuid, logging, asyncio
from datetime import datetime
from flask import Flask
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes,
    CallbackQueryHandler
)
from supabase import create_client, Client

# ========= LOGGING =========
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("recargas")

# ========= ENV =========
TG_BOT_TOKEN = os.getenv("TG_RECHARGE_BOT_TOKEN", "")
ADMIN_BOT_TOKEN = os.getenv("ADMIN_BOT_TOKEN", "")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0"))
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_API_KEY", "")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "")
PRICE_PER_CREDIT = float(os.getenv("PRICE_PER_CREDIT", "1"))
YAPE_QR_URL = os.getenv("YAPE_QR_URL", "")

if not (TG_BOT_TOKEN and ADMIN_BOT_TOKEN and ADMIN_CHAT_ID and SUPABASE_URL and SUPABASE_KEY and PUBLIC_BASE_URL and YAPE_QR_URL):
    raise SystemExit("❌ Faltan variables de entorno necesarias")

# ========= CLIENTES =========
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
user_bot = ApplicationBuilder().token(TG_BOT_TOKEN).build()
admin_bot = ApplicationBuilder().token(ADMIN_BOT_TOKEN).build()

# ========= DB HELPERS =========
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

# ========= HANDLERS =========
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 Bienvenido al Bot de Recargas\n"
        "Usa /recargar <monto>\nEjemplo: /recargar 10"
    )

async def cmd_recargar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if len(context.args) != 1:
        await update.message.reply_text("Uso correcto: /recargar <monto>. Ejemplo: /recargar 5")
        return

    try:
        amount = float(context.args[0])
        if amount <= 0:
            raise ValueError()
    except Exception:
        await update.message.reply_text("Monto inválido. Ejemplo: /recargar 5")
        return

    pago_id = str(uuid.uuid4())
    pagos_upsert(pago_id, str(user.id), user.username or "", amount)

    # QR de Yape
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📸 Subir captura de pago", callback_data=f"subir:{pago_id}")]
    ])
    await update.message.reply_photo(
        photo=YAPE_QR_URL,
        caption=f"🔗 Paga {amount:.2f} soles con Yape.\n"
                f"ID de pedido: <code>{pago_id}</code>\n\n"
                "Luego sube tu captura de pago.",
        reply_markup=kb,
        parse_mode="HTML"
    )

    # Notificar al admin
    kb_admin = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Aprobar", callback_data=f"aprobar:{pago_id}:{user.id}:{amount}")],
        [InlineKeyboardButton("❌ Rechazar", callback_data=f"rechazar:{pago_id}:{user.id}")]
    ])
    await admin_bot.bot.send_message(
        chat_id=ADMIN_CHAT_ID,
        text=f"📢 Nueva solicitud de recarga\n\n"
             f"👤 Usuario: {user.username or user.id}\n"
             f"💰 Monto: {amount:.2f} soles\n"
             f"🆔 ID: {pago_id}",
        reply_markup=kb_admin
    )

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data.split(":")

    if data[0] == "aprobar":
        _, pago_id, user_id, amount = data
        pagos_set_status(pago_id, "aprobado")
        added, new_total = user_add_credits(user_id, float(amount))

        await user_bot.bot.send_message(
            chat_id=int(user_id),
            text=f"✅ Pago aprobado. Se añadieron {added} créditos.\nSaldo actual: {new_total}"
        )
        await q.edit_message_text("✅ Recarga aprobada")

    elif data[0] == "rechazar":
        _, pago_id, user_id = data
        pagos_set_status(pago_id, "rechazado")

        await user_bot.bot.send_message(
            chat_id=int(user_id),
            text="❌ Tu recarga fue rechazada. Contacta soporte si crees que es un error."
        )
        await q.edit_message_text("❌ Recarga rechazada")

# ========= APP =========
user_bot.add_handler(CommandHandler("start", cmd_start))
user_bot.add_handler(CommandHandler("recargar", cmd_recargar))
user_bot.add_handler(CallbackQueryHandler(on_callback, pattern="^(aprobar|rechazar)"))

# Flask para healthcheck
app_flask = Flask(__name__)
@app_flask.route("/health")
def health():
    return "ok", 200

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))

    async def run_all():
        asyncio.create_task(user_bot.run_polling(close_loop=False))
        from waitress import serve
        serve(app_flask, host="0.0.0.0", port=port)

    asyncio.run(run_all())
