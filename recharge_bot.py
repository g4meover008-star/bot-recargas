import os
import io
import uuid
import asyncio
import logging
import threading
from datetime import datetime

import qrcode
import httpx
from flask import Flask, request, jsonify
from mercadopago import SDK
from supabase import create_client, Client

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, CallbackQueryHandler

# -------------------- LOGGING --------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("recargas")

# -------------------- ENV --------------------
TG_BOT_TOKEN = os.getenv("TG_RECHARGE_BOT_TOKEN", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_ANON_KEY") or os.getenv("SUPABASE_API_KEY") or ""
MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN", "")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "")
CURRENCY = os.getenv("CURRENCY", "PEN")
PRICE_PER_CREDIT = float(os.getenv("PRICE_PER_CREDIT", "25"))

# datos del bot admin
ADMIN_BOT_TOKEN = os.getenv("ADMIN_BOT_TOKEN", "")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "")

if not (TG_BOT_TOKEN and SUPABASE_URL and SUPABASE_KEY and MP_ACCESS_TOKEN and PUBLIC_BASE_URL):
    raise SystemExit("Faltan variables de entorno necesarias.")

# -------------------- CLIENTES --------------------
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
mp = SDK(MP_ACCESS_TOKEN)
app_flask = Flask(__name__)
tg_app = ApplicationBuilder().token(TG_BOT_TOKEN).build()

# Bot admin (para notificaciones)
admin_app = None
if ADMIN_BOT_TOKEN:
    admin_app = ApplicationBuilder().token(ADMIN_BOT_TOKEN).build()

# -------------------- DB helpers --------------------
def pagos_upsert(pago_id: str, user_id: str, username: str, amount: float, pref_id: str, init_point: str):
    supabase.table("pagos").upsert({
        "id": pago_id,
        "user_id": str(user_id),
        "username": username,
        "amount": amount,
        "status": "pendiente",
        "preference_id": pref_id,
        "init_point": init_point,
        "created_at": datetime.utcnow().isoformat()
    }, on_conflict="id").execute()

def pagos_get(pago_id: str):
    r = supabase.table("pagos").select("*").eq("id", pago_id).limit(1).execute()
    return r.data[0] if r.data else None

def pagos_set_status(pago_id: str, new_status: str, payment_id: str | None = None):
    data = {"status": new_status, "updated_at": datetime.utcnow().isoformat()}
    if payment_id:
        data["payment_id"] = str(payment_id)
    supabase.table("pagos").update(data).eq("id", pago_id).execute()

def user_add_credits(user_id: str, amount_paid: float):
    r = supabase.table("usuarios").select("creditos").eq("telegram_id", str(user_id)).limit(1).execute()
    current = int(r.data[0]["creditos"]) if r.data and r.data[0].get("creditos") is not None else 0
    to_add = int(round(amount_paid / PRICE_PER_CREDIT))
    new_value = current + to_add
    supabase.table("usuarios").update({"creditos": new_value}).eq("telegram_id", str(user_id)).execute()
    try:
        supabase.table("creditos_historial").insert({
            "usuario_id": str(user_id),
            "delta": to_add,
            "motivo": "recarga_mp",
            "hecho_por": "mercado_pago"
        }).execute()
    except Exception:
        pass
    return to_add, new_value

# -------------------- MP helpers --------------------
def mp_create_preference(pago_id: str, amount: float):
    pref = {
        "items": [{
            "title": "Recarga de cuentas",
            "quantity": 1,
            "currency_id": CURRENCY,
            "unit_price": float(amount)
        }],
        "external_reference": pago_id,
        "notification_url": f"{PUBLIC_BASE_URL}/mp/webhook"
    }
    resp = mp.preference().create(pref)
    return resp["response"]["id"], resp["response"]["init_point"]

def mp_get_payment(payment_id: str):
    return mp.payment().get(payment_id)

# -------------------- QR --------------------
def build_qr_png_bytes(url: str) -> bytes:
    img = qrcode.make(url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()

# -------------------- TELEGRAM --------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Bienvenido a Bot de Recargas.\n\n"
        f"Tarifa: {PRICE_PER_CREDIT:.2f} {CURRENCY} por cuenta.\n\n"
        "Usa /recargar <cantidad_de_cuentas>\n"
        "Ejemplos:\n"
        "/recargar 1 (1 cuenta)\n"
        "/recargar 5 (5 cuentas)\n\n"
        "Te daré un link de pago (y QR). Cuando MP apruebe, acreditaré créditos."
    )

async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong ✅")

async def cmd_recargar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if len(context.args) != 1:
        await update.message.reply_text("Uso: /recargar <número_de_cuentas>\nEj: /recargar 2")
        return
    try:
        cuentas = int(context.args[0])
        if cuentas <= 0:
            raise ValueError()
    except Exception:
        await update.message.reply_text("Cantidad inválida. Ej: /recargar 2")
        return

    monto = cuentas * PRICE_PER_CREDIT
    pago_id = str(uuid.uuid4())
    pref_id, init_point = mp_create_preference(pago_id, monto)
    pagos_upsert(pago_id, str(user.id), user.username or "", monto, pref_id, init_point)

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 Pagar ahora", url=init_point)],
        [InlineKeyboardButton("🧾 Ver QR", callback_data=f"qr:{pago_id}")]
    ])

    await update.message.reply_text(
        f"🔗 Link de pago por {monto:.2f} {CURRENCY}\n"
        f"Pedido: <code>{pago_id}</code>\n\n"
        f"{cuentas} cuenta(s) × {PRICE_PER_CREDIT:.2f} {CURRENCY} c/u.\n\n"
        "Si prefieres QR, pulsa “Ver QR”.",
        reply_markup=kb
    )

    # 🔔 Notificar al admin bot
    if admin_app and ADMIN_CHAT_ID:
        try:
            text = (
                f"📢 Nueva solicitud de recarga\n\n"
                f"👤 Usuario: @{user.username or user.id}\n"
                f"🆔 ID: {user.id}\n"
                f"🧾 Pedido: {pago_id}\n"
                f"💰 Monto: {monto:.2f} {CURRENCY}\n"
                f"➡️ {cuentas} cuenta(s)"
            )
            await admin_app.bot.send_message(chat_id=ADMIN_CHAT_ID, text=text)
        except Exception as e:
            log.warning(f"No se pudo notificar al admin: {e}")

async def on_qr_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    if not data.startswith("qr:"):
        return
    pago_id = data.split(":", 1)[1]
    row = pagos_get(pago_id)
    if not row:
        await q.message.reply_text("No encuentro ese pedido.")
        return
    init_point = row.get("init_point") or ""
    if not init_point:
        await q.message.reply_text("No tengo el link de pago aún.")
        return
    png = build_qr_png_bytes(init_point)
    await q.message.reply_photo(png, caption="Escanea para pagar (es el mismo link).")

# Handlers
tg_app.add_handler(CommandHandler("start", cmd_start))
tg_app.add_handler(CommandHandler("ping", cmd_ping))
tg_app.add_handler(CommandHandler("recargar", cmd_recargar))
tg_app.add_handler(CallbackQueryHandler(on_qr_callback, pattern=r"^qr:"))

# -------------------- FLASK --------------------
@app_flask.get("/health")
def health():
    return "ok", 200

# -------------------- MAIN --------------------
def create_app():
    def _start_telegram_polling():
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            tg_app.run_polling(close_loop=False)
        except Exception:
            log.exception("Fallo al iniciar el polling de Telegram")
    threading.Thread(target=_start_telegram_polling, daemon=True).start()
    return app_flask

if __name__ == "__main__":
    from waitress import serve
    port = int(os.getenv("PORT", "8080"))
    threading.Thread(target=lambda: tg_app.run_polling(close_loop=False), daemon=True).start()
    log.info(f"Sirviendo Flask en 0.0.0.0:{port}")
    serve(app_flask, host="0.0.0.0", port=port)
