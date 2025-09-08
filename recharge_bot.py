import os
import io
import uuid
import asyncio
import logging
import qrcode
from datetime import datetime
from flask import Flask, request, jsonify

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, CallbackQueryHandler

# 🚀 Supabase estable (0.7.1)
from supabase import create_client, Client

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("recargas")

# ========= ENV =========
TG_BOT_TOKEN = os.getenv("TG_RECHARGE_BOT_TOKEN", "")   # token del BOT DE RECARGAS
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")  # 👈 usa SUPABASE_KEY directamente
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "")  # ej: https://tu-app.up.railway.app
CURRENCY = os.getenv("CURRENCY", "PEN")             # Moneda (PEN soles)
PRICE_PER_CREDIT = float(os.getenv("PRICE_PER_CREDIT", "1"))  # 1 sol = 1 crédito

if not (TG_BOT_TOKEN and SUPABASE_URL and SUPABASE_KEY and PUBLIC_BASE_URL):
    raise SystemExit("❌ Faltan variables de entorno: TG_RECHARGE_BOT_TOKEN, SUPABASE_URL, SUPABASE_KEY, PUBLIC_BASE_URL")

# ========= CLIENTES =========
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
app_flask = Flask(__name__)

# Telegram app global
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
    }).execute()

def pagos_set_status(pago_id: str, new_status: str):
    data = {"status": new_status, "updated_at": datetime.utcnow().isoformat()}
    supabase.table("pagos").update(data).eq("id", pago_id).execute()

def pagos_get(pago_id: str):
    r = supabase.table("pagos").select("*").eq("id", pago_id).limit(1).execute()
    return r.data[0] if r.data else None

def user_add_credits(user_id: str, amount_paid: float):
    """ Por defecto 1 sol = 1 crédito (PRICE_PER_CREDIT). """
    r = supabase.table("usuarios").select("creditos").eq("telegram_id", str(user_id)).limit(1).execute()
    current = int(r.data[0]["creditos"]) if r.data and r.data[0].get("creditos") is not None else 0
    to_add = int(round(amount_paid / PRICE_PER_CREDIT))
    new_value = current + to_add
    supabase.table("usuarios").update({"creditos": new_value}).eq("telegram_id", str(user_id)).execute()
    # Historial (opcional)
    try:
        supabase.table("creditos_historial").insert({
            "usuario_id": str(user_id),
            "delta": to_add,
            "motivo": "recarga_manual",
            "hecho_por": "yape_qr"
        }).execute()
    except Exception:
        pass
    return to_add, new_value

# ========= QR del link =========
def build_qr_png_bytes(path: str) -> bytes:
    img = qrcode.make(path)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()

# ========= TELEGRAM HANDLERS =========
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 Bot de Recargas\n"
        "Usa /recargar <monto>\n\n"
        "Ejemplos:\n"
        "/recargar 5  (5 soles)\n"
        "/recargar 10 (10 soles)\n\n"
        "Recibirás el QR para pagar. Luego envía la captura del pago y un admin lo confirmará."
    )

async def cmd_recargar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if len(context.args) != 1:
        await update.message.reply_text("Uso: /recargar <monto_en_soles>\nEj: /recargar 5")
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

    # Generar QR (usamos una imagen fija que subiste a Railway o link)
    qr_path = os.getenv("YAPE_QR_URL", "https://i.imgur.com/xxxxx.png")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📸 Ya pagué (enviar captura)", callback_data=f"confirma:{pago_id}")]
    ])

    await update.message.reply_photo(
        qr_path,
        caption=(
            f"🧾 Solicitud de recarga\n"
            f"Monto: {amount:.2f} {CURRENCY}\n"
            f"ID de pedido: <code>{pago_id}</code>\n\n"
            "Paga usando este QR y luego envía la captura.\n"
            "Un admin confirmará tu pago."
        ),
        reply_markup=kb
    )

async def on_confirma_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    if not data.startswith("confirma:"):
        return
    pago_id = data.split(":", 1)[1]
    row = pagos_get(pago_id)
    if not row:
        await q.message.reply_text("No encuentro ese pedido.")
        return

    # Aquí notificarías a los admins (igual que tu bot de reemplazos)
    await q.message.reply_text(
        f"📩 Admins han sido notificados del pago {pago_id}. Espera confirmación."
    )

tg_app.add_handler(CommandHandler("start", cmd_start))
tg_app.add_handler(CommandHandler("recargar", cmd_recargar))
tg_app.add_handler(CallbackQueryHandler(on_confirma_callback, pattern=r"^confirma:"))

# ========= FLASK WEB =========
@app_flask.route("/health", methods=["GET"])
def health():
    return "ok", 200

# ========= MAIN =========
if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))

    async def run_all():
        # arranca el bot de TG en segundo plano
        asyncio.create_task(tg_app.run_polling(close_loop=False))
        # arranca Flask (servidor http) en el hilo principal
        from waitress import serve
        serve(app_flask, host="0.0.0.0", port=port)

    asyncio.run(run_all())
