import os
import io
import uuid
import asyncio
import logging
from datetime import datetime

from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Bot
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, CallbackQueryHandler
from supabase import create_client, Client

# ================== LOGS ==================
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("recargas")

# ================== ENV VARS ==================
TG_BOT_TOKEN = os.getenv("TG_RECHARGE_BOT_TOKEN", "")
ADMIN_BOT_TOKEN = os.getenv("ADMIN_BOT_TOKEN", "")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0"))
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_API_KEY", "")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "")
YAPE_QR_URL = os.getenv("YAPE_QR_URL", "")
BRAND_NAME = os.getenv("BRAND_NAME", "MiServicio")
PRICE_PER_CREDIT = float(os.getenv("PRICE_PER_CREDIT", "1"))

if not (TG_BOT_TOKEN and ADMIN_BOT_TOKEN and ADMIN_CHAT_ID and SUPABASE_URL and SUPABASE_KEY):
    raise SystemExit("❌ Faltan variables de entorno necesarias")

# ================== CLIENTES ==================
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
app_flask = Flask(__name__)

# Bots
tg_app = ApplicationBuilder().token(TG_BOT_TOKEN).build()
admin_bot = Bot(ADMIN_BOT_TOKEN)

# ================== DB HELPERS ==================
def user_add_credits(user_id: str, amount_paid: float):
    r = supabase.table("usuarios").select("creditos").eq("telegram_id", str(user_id)).limit(1).execute()
    current = int(r.data[0]["creditos"]) if r.data and r.data[0].get("creditos") else 0
    to_add = int(round(amount_paid / PRICE_PER_CREDIT))
    new_value = current + to_add
    supabase.table("usuarios").update({"creditos": new_value}).eq("telegram_id", str(user_id)).execute()

    # Historial
    try:
        supabase.table("creditos_historial").insert({
            "usuario_id": str(user_id),
            "delta": to_add,
            "motivo": "recarga_yape",
            "hecho_por": "admin"
        }).execute()
    except Exception:
        pass
    return to_add, new_value

# ================== TELEGRAM HANDLERS ==================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"🤖 Bienvenido a {BRAND_NAME}\n\n"
        "Usa /recargar <monto>\n\n"
        "Ejemplo:\n"
        "/recargar 10 (para 10 créditos)"
    )

async def cmd_recargar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if len(context.args) != 1:
        await update.message.reply_text("Uso: /recargar <monto>\nEj: /recargar 10")
        return

    try:
        amount = float(context.args[0])
        if amount <= 0:
            raise ValueError()
    except Exception:
        await update.message.reply_text("Monto inválido. Ej: /recargar 5")
        return

    pago_id = str(uuid.uuid4())

    # Mensaje al usuario
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📸 Ver QR de Yape", url=YAPE_QR_URL)]
    ])
    await update.message.reply_text(
        f"💳 Para recargar {amount:.2f} soles, paga con Yape.\n"
        f"📌 ID de pedido: <code>{pago_id}</code>\n\n"
        f"👉 Envía la captura del pago al admin.\n"
        "Tus créditos serán acreditados cuando el admin confirme ✅",
        reply_markup=kb
    )

    # Notificar al admin
    akb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Aprobar", callback_data=f"approve:{pago_id}:{user.id}:{amount}"),
            InlineKeyboardButton("❌ Rechazar", callback_data=f"reject:{pago_id}:{user.id}:{amount}")
        ]
    ])
    await admin_bot.send_message(
        chat_id=ADMIN_CHAT_ID,
        text=(
            f"📢 Nueva solicitud de recarga\n\n"
            f"👤 Usuario: @{user.username or 'sin_username'} ({user.id})\n"
            f"💰 Monto: {amount:.2f} soles\n"
            f"🆔 Pedido: {pago_id}\n\n"
            "¿Aprobar la recarga?"
        ),
        reply_markup=akb,
        parse_mode="HTML"
    )

async def on_admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data.split(":")
    action, pago_id, user_id, amount = data[0], data[1], int(data[2]), float(data[3])

    if action == "approve":
        added, new_total = user_add_credits(user_id, amount)
        await tg_app.bot.send_message(
            chat_id=user_id,
            text=(
                f"✅ Tu recarga de {amount:.2f} soles fue aprobada.\n"
                f"💳 Créditos añadidos: {added}\n"
                f"📌 Nuevo saldo: {new_total}"
            )
        )
        await q.edit_message_text(f"✅ Recarga aprobada para {user_id} ({amount:.2f} soles)")
    else:
        await tg_app.bot.send_message(
            chat_id=user_id,
            text="❌ Tu recarga fue rechazada. Contacta al admin."
        )
        await q.edit_message_text(f"❌ Recarga rechazada para {user_id} ({amount:.2f} soles)")

# ================== ROUTES ==================
@app_flask.route("/health", methods=["GET"])
def health():
    return "ok", 200

# ================== MAIN ==================
if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))

    tg_app.add_handler(CommandHandler("start", cmd_start))
    tg_app.add_handler(CommandHandler("recargar", cmd_recargar))
    tg_app.add_handler(CallbackQueryHandler(on_admin_callback, pattern="^(approve|reject):"))

    async def run_all():
        asyncio.create_task(tg_app.run_polling(close_loop=False))
        from waitress import serve
        serve(app_flask, host="0.0.0.0", port=port)

    asyncio.run(run_all())
