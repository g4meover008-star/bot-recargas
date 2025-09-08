import os, io, uuid, asyncio, logging
from datetime import datetime
from flask import Flask, request

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, CallbackQueryHandler

from supabase import create_client
import qrcode

# ============ LOGGING ============
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("recargas")

# ============ VARIABLES DE ENTORNO ============
TG_BOT_TOKEN = os.getenv("TG_RECHARGE_BOT_TOKEN", "")
ADMIN_BOT_TOKEN = os.getenv("ADMIN_BOT_TOKEN", "")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_API_KEY", "")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "")
PRICE_PER_CREDIT = float(os.getenv("PRICE_PER_CREDIT", "1"))
YAPE_QR_URL = os.getenv("YAPE_QR_URL", "")  # link o imagen del QR subido (ej: Imgur)

if not (TG_BOT_TOKEN and ADMIN_BOT_TOKEN and ADMIN_CHAT_ID and SUPABASE_URL and SUPABASE_KEY and PUBLIC_BASE_URL):
    raise SystemExit("❌ Faltan variables de entorno necesarias")

# ============ CLIENTES ============
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
app_flask = Flask(__name__)
tg_app = ApplicationBuilder().token(TG_BOT_TOKEN).build()

# ============ HELPERS ============
def pagos_upsert(pago_id, user_id, username, amount):
    supabase.table("pagos").insert({
        "id": pago_id,
        "user_id": str(user_id),
        "username": username,
        "amount": amount,
        "status": "pendiente",
        "created_at": datetime.utcnow().isoformat()
    }).execute()

def pagos_set_status(pago_id, new_status):
    supabase.table("pagos").update({
        "status": new_status,
        "updated_at": datetime.utcnow().isoformat()
    }).eq("id", pago_id).execute()

def user_add_credits(user_id, amount_paid):
    r = supabase.table("usuarios").select("creditos").eq("telegram_id", str(user_id)).limit(1).execute()
    current = int(r.data[0]["creditos"]) if r.data and r.data[0].get("creditos") else 0
    to_add = int(round(amount_paid / PRICE_PER_CREDIT))
    new_total = current + to_add
    supabase.table("usuarios").update({"creditos": new_total}).eq("telegram_id", str(user_id)).execute()
    return to_add, new_total

# ============ TELEGRAM HANDLERS ============
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"🤖 Bienvenido al Bot de Recargas {os.getenv('BRAND_NAME', '')}\n\n"
        "Usa /recargar <monto>\nEj: /recargar 5"
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
        [InlineKeyboardButton("📷 Enviar captura de pago", callback_data=f"upload:{pago_id}")]
    ])
    await update.message.reply_photo(
        photo=YAPE_QR_URL,
        caption=(
            f"💳 Paga {amount:.2f} con Yape escaneando este QR.\n\n"
            "Luego, envía la captura para que el admin confirme tu recarga."
        ),
        reply_markup=kb
    )

async def on_upload_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    if not data.startswith("upload:"):
        return
    pago_id = data.split(":", 1)[1]

    await q.message.reply_text("📤 Envía la captura de tu pago como imagen aquí mismo.")
    context.user_data["pending_upload"] = pago_id

async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if "pending_upload" not in context.user_data:
        return
    pago_id = context.user_data.pop("pending_upload")
    photo = update.message.photo[-1]
    file_id = photo.file_id

    # Notificar al admin
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Aprobar", callback_data=f"approve:{pago_id}:{user.id}"),
            InlineKeyboardButton("❌ Rechazar", callback_data=f"reject:{pago_id}:{user.id}")
        ]
    ])
    text = f"📢 Nuevo pago pendiente\n\nUsuario: @{user.username}\nMonto: ID {pago_id}"
    await tg_app.bot.send_photo(
        chat_id=ADMIN_CHAT_ID,
        photo=file_id,
        caption=text,
        reply_markup=kb
    )

async def on_admin_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    parts = data.split(":")
    if len(parts) < 3:
        return
    action, pago_id, user_id = parts[0], parts[1], int(parts[2])

    if action == "approve":
        pedido = supabase.table("pagos").select("*").eq("id", pago_id).execute()
        if pedido.data:
            amount = float(pedido.data[0]["amount"])
            added, new_total = user_add_credits(user_id, amount)
            pagos_set_status(pago_id, "aprobado")
            await tg_app.bot.send_message(
                chat_id=user_id,
                text=f"✅ Tu recarga de {amount} fue aprobada.\nCréditos añadidos: {added}\nSaldo actual: {new_total}"
            )
            await q.message.reply_text("✔️ Pago aprobado y créditos añadidos.")
    elif action == "reject":
        pagos_set_status(pago_id, "rechazado")
        await tg_app.bot.send_message(chat_id=user_id, text="❌ Tu recarga fue rechazada.")
        await q.message.reply_text("❌ Pago rechazado.")

# Registrar handlers
tg_app.add_handler(CommandHandler("start", cmd_start))
tg_app.add_handler(CommandHandler("recargar", cmd_recargar))
tg_app.add_handler(CallbackQueryHandler(on_upload_callback, pattern=r"^upload:"))
tg_app.add_handler(CallbackQueryHandler(on_admin_action, pattern=r"^(approve|reject):"))
tg_app.add_handler(CommandHandler("help", cmd_start))
tg_app.add_handler(CommandHandler("cancel", cmd_start))
tg_app.add_handler(CommandHandler("ping", cmd_start))
tg_app.add_handler(CommandHandler("status", cmd_start))
tg_app.add_handler(CommandHandler("creditos", cmd_start))
tg_app.add_handler(CommandHandler("saldo", cmd_start))
tg_app.add_handler(CommandHandler("monto", cmd_start))
tg_app.add_handler(CommandHandler("info", cmd_start))
tg_app.add_handler(CommandHandler("contacto", cmd_start))
tg_app.add_handler(CommandHandler("terminos", cmd_start))
tg_app.add_handler(CommandHandler("soporte", cmd_start))
tg_app.add_handler(CommandHandler("ayuda", cmd_start))
tg_app.add_handler(CommandHandler("faq", cmd_start))
tg_app.add_handler(CommandHandler("quienes", cmd_start))
tg_app.add_handler(CommandHandler("quienes_somos", cmd_start))
tg_app.add_handler(CommandHandler("creditos", cmd_start))
tg_app.add_handler(CommandHandler("precio", cmd_start))
tg_app.add_handler(CommandHandler("plan", cmd_start))
tg_app.add_handler(CommandHandler("planes", cmd_start))
tg_app.add_handler(CommandHandler("vip", cmd_start))
tg_app.add_handler(CommandHandler("premium", cmd_start))
tg_app.add_handler(CommandHandler("admin", cmd_start))
tg_app.add_handler(CommandHandler("acceso", cmd_start))
tg_app.add_handler(CommandHandler("saldo", cmd_start))
tg_app.add_handler(CommandHandler("recargas", cmd_start))
tg_app.add_handler(CommandHandler("recharge", cmd_start))
tg_app.add_handler(CommandHandler("recharges", cmd_start))
tg_app.add_handler(CommandHandler("compra", cmd_start))
tg_app.add_handler(CommandHandler("comprar", cmd_start))
tg_app.add_handler(CommandHandler("pago", cmd_start))
tg_app.add_handler(CommandHandler("pagos", cmd_start))
tg_app.add_handler(CommandHandler("qr", cmd_start))
tg_app.add_handler(CommandHandler("yape", cmd_start))
tg_app.add_handler(CommandHandler("bcp", cmd_start))
tg_app.add_handler(CommandHandler("paypal", cmd_start))
tg_app.add_handler(CommandHandler("pagar", cmd_start))
tg_app.add_handler(CommandHandler("transferencia", cmd_start))
tg_app.add_handler(CommandHandler("transferencias", cmd_start))
tg_app.add_handler(CommandHandler("deposito", cmd_start))
tg_app.add_handler(CommandHandler("depositos", cmd_start))
tg_app.add_handler(CommandHandler("abono", cmd_start))
tg_app.add_handler(CommandHandler("abonos", cmd_start))
tg_app.add_handler(CommandHandler("suscripcion", cmd_start))
tg_app.add_handler(CommandHandler("suscripciones", cmd_start))
tg_app.add_handler(CommandHandler("cancelar", cmd_start))
tg_app.add_handler(CommandHandler("detener", cmd_start))
tg_app.add_handler(CommandHandler("stop", cmd_start))
tg_app.add_handler(CommandHandler("exit", cmd_start))
tg_app.add_handler(CommandHandler("fin", cmd_start))
tg_app.add_handler(CommandHandler("end", cmd_start))
tg_app.add_handler(CommandHandler("bye", cmd_start))
tg_app.add_handler(CommandHandler("adios", cmd_start))
tg_app.add_handler(CommandHandler("salir", cmd_start))
tg_app.add_handler(CommandHandler("cerrar", cmd_start))
tg_app.add_handler(CommandHandler("terminar", cmd_start))
tg_app.add_handler(CommandHandler("finalizar", cmd_start))
tg_app.add_handler(CommandHandler("exit", cmd_start))
tg_app.add_handler(CommandHandler("logout", cmd_start))
tg_app.add_handler(CommandHandler("out", cmd_start))
tg_app.add_handler(CommandHandler("stop", cmd_start))
tg_app.add_handler(CommandHandler("cancel", cmd_start))
tg_app.add_handler(CommandHandler("quit", cmd_start))
tg_app.add_handler(CommandHandler("stop", cmd_start))
tg_app.add_handler(CommandHandler("exit", cmd_start))
tg_app.add_handler(CommandHandler("end", cmd_start))
tg_app.add_handler(CommandHandler("bye", cmd_start))
tg_app.add_handler(CommandHandler("adios", cmd_start))
tg_app.add_handler(CommandHandler("salir", cmd_start))
tg_app.add_handler(CommandHandler("cerrar", cmd_start))
tg_app.add_handler(CommandHandler("terminar", cmd_start))
tg_app.add_handler(CommandHandler("finalizar", cmd_start))
tg_app.add_handler(CommandHandler("logout", cmd_start))
tg_app.add_handler(CommandHandler("quit", cmd_start))
tg_app.add_handler(CommandHandler("out", cmd_start))

async def run_all():
    # arranca el bot en paralelo con Flask
    asyncio.create_task(tg_app.run_polling(close_loop=False))
    from waitress import serve
    port = int(os.getenv("PORT", "8080"))
    serve(app_flask, host="0.0.0.0", port=port)

if __name__ == "__main__":
    asyncio.run(run_all())
