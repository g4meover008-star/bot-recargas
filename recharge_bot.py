import os
import io
import uuid
import asyncio
import logging
import threading
from datetime import datetime
from supabase import create_client, Client

import qrcode
from flask import Flask, request, jsonify
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)

# -------------------- LOGGING --------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("recargas")

# -------------------- ENV --------------------
TG_BOT_TOKEN = os.getenv("TG_RECHARGE_BOT_TOKEN", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_API_KEY", "")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0"))  # ID de admin
YAPE_QR_URL = os.getenv("YAPE_QR_URL", "")
PRICE_PER_CREDIT = float(os.getenv("PRICE_PER_CREDIT", "25"))  # precio por cuenta

if not (TG_BOT_TOKEN and SUPABASE_URL and SUPABASE_KEY and ADMIN_CHAT_ID and YAPE_QR_URL):
    raise SystemExit("❌ Faltan variables de entorno necesarias")

# -------------------- CLIENTES --------------------
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
app_flask = Flask(__name__)
tg_app = ApplicationBuilder().token(TG_BOT_TOKEN).build()

# -------------------- DB helpers --------------------
def pagos_upsert(pago_id: str, user_id: str, username: str, cuentas: int, total: float):
    supabase.table("pagos").upsert(
        {
            "id": pago_id,
            "user_id": str(user_id),
            "username": username,
            "cuentas": cuentas,
            "monto": total,
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
    current = int(r.data[0]["creditos"]) if r.data and r.data[0].get("creditos") else 0
    new_value = current + cuentas
    supabase.table("usuarios").update({"creditos": new_value}).eq("telegram_id", str(user_id)).execute()
    return new_value

# -------------------- HANDLERS --------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("💳 Créditos", callback_data="comprar")]])
    await update.message.reply_text(
        "👋 Bienvenido.\n"
        f"Tarifa: {PRICE_PER_CREDIT:.2f} PEN por cuenta.\n\n"
        "Selecciona una opción:",
        reply_markup=kb,
    )

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "comprar":
        await query.message.reply_text(
            f"Tarifa: {PRICE_PER_CREDIT:.2f} PEN por cuenta.\n"
            "¿Cuántas cuentas deseas comprar?"
        )
        context.user_data["esperando_cantidad"] = True

async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("esperando_cantidad"):
        try:
            cuentas = int(update.message.text.strip())
            if cuentas <= 0:
                raise ValueError()

            total = cuentas * PRICE_PER_CREDIT
            pago_id = str(uuid.uuid4())

            # Guardar en BD
            pagos_upsert(pago_id, update.effective_user.id, update.effective_user.username or "", cuentas, total)

            # Enviar QR y pedido
            await update.message.reply_photo(
                photo=YAPE_QR_URL,
                caption=(
                    f"💳 Pedido: <code>{pago_id}</code>\n"
                    f"📦 {cuentas} cuentas = {total:.2f} PEN\n\n"
                    "➡️ Escanea el QR y envía el comprobante aquí mismo."
                ),
                parse_mode="HTML",
            )

            # Notificar al admin
            kb_admin = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Confirmar", callback_data=f"confirm:{pago_id}:{update.effective_user.id}:{cuentas}")]
            ])
            await tg_app.bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=f"📢 Nuevo pedido\n\nID: {pago_id}\nUsuario: {update.effective_user.username or update.effective_user.id}\nCuentas: {cuentas}\nTotal: {total:.2f} PEN",
                reply_markup=kb_admin,
            )

            context.user_data["esperando_cantidad"] = False

        except Exception:
            await update.message.reply_text("❌ Ingresa un número válido de cuentas.")

async def on_admin_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data.startswith("confirm:"):
        _, pago_id, user_id, cuentas = query.data.split(":")
        cuentas = int(cuentas)

        pagos_set_status(pago_id, "aprobado")
        new_total = user_add_credits(user_id, cuentas)

        # Notificar al cliente
        await tg_app.bot.send_message(
            chat_id=int(user_id),
            text=f"✅ Tu pago fue confirmado.\nSe acreditaron {cuentas} créditos.\nSaldo actual: {new_total}."
        )

        # Confirmación al admin
        await query.message.reply_text("✔ Pago confirmado y créditos acreditados.")

# -------------------- FLASK --------------------
@app_flask.get("/health")
def health():
    return "ok", 200

# -------------------- TELEGRAM HANDLERS --------------------
tg_app.add_handler(CommandHandler("start", cmd_start))
tg_app.add_handler(CallbackQueryHandler(on_callback, pattern="^comprar$"))
tg_app.add_handler(CallbackQueryHandler(on_admin_confirm, pattern="^confirm:"))
tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

# -------------------- FACTORY --------------------
def create_app():
    def _start_telegram_polling():
        try:
            tg_app.run_polling(close_loop=False)
        except Exception:
            log.exception("Fallo al iniciar polling de Telegram")

    threading.Thread(target=_start_telegram_polling, name="tg-polling", daemon=True).start()
    return app_flask

# -------------------- MAIN --------------------
if __name__ == "__main__":
    from waitress import serve
    port = int(os.getenv("PORT", "8080"))
    threading.Thread(target=lambda: tg_app.run_polling(close_loop=False), daemon=True).start()
    log.info(f"🌐 Servidor Flask en http://0.0.0.0:{port}")
    serve(app_flask, host="0.0.0.0", port=port)

# 👇 Railway usará esto
app = create_app()
