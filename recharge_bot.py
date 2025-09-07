import os
import uuid
import asyncio
import logging
from datetime import datetime

import httpx
from flask import Flask
from supabase import create_client, Client
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
)

# -------------------- LOGGING --------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("recargas")

# -------------------- ENV --------------------
TG_BOT_TOKEN = os.getenv("TG_RECHARGE_BOT_TOKEN", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_API_KEY", "")
PRICE_PER_CREDIT = float(os.getenv("PRICE_PER_CREDIT", "25"))
YAPE_QR_URL = os.getenv("YAPE_QR_URL", "")
CURRENCY = os.getenv("CURRENCY", "PEN")

# Admin
ADMIN_BOT_TOKEN = os.getenv("ADMIN_BOT_TOKEN", "")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "")

if not (TG_BOT_TOKEN and SUPABASE_URL and SUPABASE_KEY and ADMIN_BOT_TOKEN and ADMIN_CHAT_ID and YAPE_QR_URL):
    raise SystemExit("❌ Faltan variables de entorno necesarias")

# -------------------- CLIENTES --------------------
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
app_flask = Flask(__name__)

tg_app = ApplicationBuilder().token(TG_BOT_TOKEN).build()
admin_app = ApplicationBuilder().token(ADMIN_BOT_TOKEN).build()

# -------------------- DB helpers --------------------
def pagos_upsert(pago_id: str, user_id: str, username: str, cuentas: int, monto: float):
    supabase.table("pagos").upsert(
        {
            "id": pago_id,
            "user_id": str(user_id),
            "username": username,
            "cuentas": cuentas,
            "amount": monto,
            "status": "pendiente",
            "created_at": datetime.utcnow().isoformat(),
        },
        on_conflict="id",
    ).execute()

def pagos_set_status(pago_id: str, new_status: str):
    supabase.table("pagos").update(
        {"status": new_status, "updated_at": datetime.utcnow().isoformat()}
    ).eq("id", pago_id).execute()

def user_add_credits(user_id: str, cuentas: int):
    r = supabase.table("usuarios").select("creditos").eq("telegram_id", str(user_id)).limit(1).execute()
    current = int(r.data[0]["creditos"]) if r.data and r.data[0].get("creditos") is not None else 0
    new_value = current + cuentas
    supabase.table("usuarios").update({"creditos": new_value}).eq("telegram_id", str(user_id)).execute()
    return cuentas, new_value

# -------------------- TELEGRAM CLIENTE --------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"👋 Bienvenido a Bot de Recargas.\n\n"
        f"Tarifa: {PRICE_PER_CREDIT:.2f} {CURRENCY} por cuenta.\n\n"
        f"Usa /recargar <n_cuentas>\n"
        f"Ejemplos:\n"
        f"/recargar 1  (1 cuenta)\n"
        f"/recargar 5  (5 cuentas)\n\n"
        f"Te mostraré un QR de Yape para pagar."
    )

async def cmd_recargar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if len(context.args) != 1:
        await update.message.reply_text("Uso: /recargar <n_cuentas>\nEj: /recargar 2")
        return

    try:
        cuentas = int(context.args[0])
        if cuentas <= 0:
            raise ValueError()
    except Exception:
        await update.message.reply_text("Número inválido. Ej: /recargar 2")
        return

    monto_total = cuentas * PRICE_PER_CREDIT
    pago_id = str(uuid.uuid4())
    pagos_upsert(pago_id, str(user.id), user.username or "", cuentas, monto_total)

    # Responde al cliente
    await update.message.reply_text(
        f"💳 Para recargar {cuentas} cuenta(s):\n"
        f"Monto a enviar: {monto_total:.2f} {CURRENCY}\n\n"
        f"Escanea este QR de Yape y envíame el comprobante:",
    )
    await update.message.reply_photo(YAPE_QR_URL, caption="📲 Escanea con Yape")

    # Notifica al admin
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Confirmar", callback_data=f"ok:{pago_id}:{user.id}:{cuentas}")],
        [InlineKeyboardButton("❌ Rechazar", callback_data=f"no:{pago_id}:{user.id}")]
    ])
    await admin_app.bot.send_message(
        chat_id=ADMIN_CHAT_ID,
        text=(
            f"📢 Nueva solicitud de recarga\n\n"
            f"Usuario: @{user.username or 'sin_username'} ({user.id})\n"
            f"Cuentas: {cuentas}\n"
            f"Monto: {monto_total:.2f} {CURRENCY}\n"
            f"ID pedido: <code>{pago_id}</code>"
        ),
        reply_markup=kb,
        parse_mode="HTML",
    )

tg_app.add_handler(CommandHandler("start", cmd_start))
tg_app.add_handler(CommandHandler("recargar", cmd_recargar))

# -------------------- TELEGRAM ADMIN --------------------
async def on_admin_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data.split(":")
    action, pago_id, user_id = data[0], data[1], data[2]
    cuentas = int(data[3]) if len(data) > 3 else 0

    if action == "ok":
        pagos_set_status(pago_id, "aprobado")
        added, new_total = user_add_credits(user_id, cuentas)
        await tg_app.bot.send_message(
            chat_id=int(user_id),
            text=f"✅ Recarga confirmada.\nSe acreditaron {added} créditos.\nNuevo saldo: {new_total}"
        )
        await q.edit_message_text(f"✅ Pago confirmado para usuario {user_id}")
    elif action == "no":
        pagos_set_status(pago_id, "rechazado")
        await tg_app.bot.send_message(
            chat_id=int(user_id),
            text="❌ Tu pago fue rechazado. Contacta soporte."
        )
        await q.edit_message_text(f"❌ Pago rechazado para usuario {user_id}")

admin_app.add_handler(CallbackQueryHandler(on_admin_action, pattern=r"^(ok|no):"))

# -------------------- FACTORY --------------------
def create_app():
    loop = asyncio.get_event_loop()
    loop.create_task(tg_app.run_polling(close_loop=False))
    loop.create_task(admin_app.run_polling(close_loop=False))
    return app_flask

if __name__ == "__main__":
    from waitress import serve
    port = int(os.getenv("PORT", "8080"))

    loop = asyncio.get_event_loop()
    loop.create_task(tg_app.run_polling(close_loop=False))
    loop.create_task(admin_app.run_polling(close_loop=False))

    log.info(f"Sirviendo Flask en 0.0.0.0:{port}")
    serve(app_flask, host="0.0.0.0", port=port)
