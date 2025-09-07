import os, uuid, logging
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, CallbackQueryHandler, ContextTypes
from supabase import create_client, Client

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("recargas")

# ========= ENV =========
TG_BOT_TOKEN = os.getenv("TG_RECHARGE_BOT_TOKEN", "")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0"))   # tu chat ID de admin
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_API_KEY") or ""
PRICE_PER_CREDIT = float(os.getenv("PRICE_PER_CREDIT", "1"))

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
tg_app = ApplicationBuilder().token(TG_BOT_TOKEN).build()

# ========= DB helpers =========
def pagos_upsert(pago_id, user_id, username, amount):
    supabase.table("pagos").upsert({
        "id": pago_id,
        "user_id": str(user_id),
        "username": username,
        "amount": amount,
        "status": "pendiente",
        "created_at": datetime.utcnow().isoformat()
    }, on_conflict="id").execute()

def pagos_set_status(pago_id, status):
    supabase.table("pagos").update({"status": status}).eq("id", pago_id).execute()

def user_add_credits(user_id: str, amount: float):
    r = supabase.table("usuarios").select("creditos").eq("telegram_id", str(user_id)).limit(1).execute()
    current = int(r.data[0]["creditos"]) if r.data else 0
    to_add = int(round(amount / PRICE_PER_CREDIT))
    new_value = current + to_add
    supabase.table("usuarios").update({"creditos": new_value}).eq("telegram_id", str(user_id)).execute()
    return to_add, new_value

# ========= HANDLERS =========
async def cmd_recargar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if len(context.args) != 1:
        await update.message.reply_text("Uso: /recargar <monto>. Ej: /recargar 5")
        return
    
    try:
        amount = float(context.args[0])
    except:
        await update.message.reply_text("Monto inválido.")
        return

    pago_id = str(uuid.uuid4())
    pagos_upsert(pago_id, user.id, user.username or "", amount)

    await update.message.reply_photo(
        photo=open("static/yape_qr.png", "rb"),  # tu QR guardado en carpeta
        caption=f"📲 Paga {amount:.2f} soles escaneando este QR.\n"
                f"ID de pago: {pago_id}\n\n"
                "Después de pagar, envía una captura aquí 📷"
    )

# Guardar captura y reenviar a admin
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    pago_id = str(uuid.uuid4())  # podrías buscar el último pago pendiente en DB

    file_id = update.message.photo[-1].file_id
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Aprobar", callback_data=f"ok:{user.id}:{pago_id}")],
        [InlineKeyboardButton("❌ Rechazar", callback_data=f"no:{user.id}:{pago_id}")]
    ])

    # notificación al admin
    await context.bot.send_photo(
        chat_id=ADMIN_CHAT_ID,
        photo=file_id,
        caption=f"📥 Nuevo pago\nUsuario: @{user.username}\nMonto: (ver DB)\nPago ID: {pago_id}",
        reply_markup=kb
    )

    # notificación al usuario
    await update.message.reply_text("📤 Enviamos tu comprobante al admin, espera confirmación.")

# Admin confirma/rechaza
async def on_admin_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    action, user_id, pago_id = q.data.split(":")
    user_id = int(user_id)

    if action == "ok":
        to_add, new_total = user_add_credits(user_id, 5)  # aquí deberías consultar el monto en DB
        pagos_set_status(pago_id, "aprobado")
        await context.bot.send_message(chat_id=user_id, text=f"✅ Recarga aprobada. Créditos añadidos: {to_add}. Total: {new_total}")
        await q.edit_message_caption(q.message.caption + "\n\n✅ Aprobado")
    else:
        pagos_set_status(pago_id, "rechazado")
        await context.bot.send_message(chat_id=user_id, text="❌ Tu recarga fue rechazada.")
        await q.edit_message_caption(q.message.caption + "\n\n❌ Rechazado")

# ========= MAIN =========
tg_app.add_handler(CommandHandler("recargar", cmd_recargar))
tg_app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
tg_app.add_handler(CallbackQueryHandler(on_admin_action, pattern="^(ok|no):"))

if __name__ == "__main__":
    tg_app.run_polling()
