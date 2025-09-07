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
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "")   # ej: https://web-production-xxxx.up.railway.app
CURRENCY = os.getenv("CURRENCY", "PEN")
COSTO_CUENTA = 25  # 💰 Cada cuenta cuesta 25 soles

if not (TG_BOT_TOKEN and SUPABASE_URL and SUPABASE_KEY and PUBLIC_BASE_URL):
    raise SystemExit("❌ Faltan variables de entorno necesarias")

# Apaga proxies heredados (bug en Railway con httpx)
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
    os.environ.pop(_k, None)

# -------------------- CLIENTES --------------------
_httpx = httpx.Client(timeout=30.0)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY, options={"http_client": _httpx})

app_flask = Flask(__name__)
tg_app = ApplicationBuilder().token(TG_BOT_TOKEN).build()

# -------------------- CONFIG --------------------
ADMIN_ID = 2016769834  # 👤 Tu ID de admin


# -------------------- DB helpers --------------------
def pagos_upsert(pago_id: str, user_id: str, username: str, cuentas: int, total: float):
    supabase.table("pagos").upsert({
        "id": pago_id,
        "user_id": str(user_id),
        "username": username,
        "cuentas": cuentas,
        "amount": total,
        "status": "pendiente",
        "created_at": datetime.utcnow().isoformat()
    }, on_conflict="id").execute()

def pagos_set_status(pago_id: str, new_status: str, admin_id: str):
    data = {
        "status": new_status,
        "confirmed_by": str(admin_id),
        "confirmed_at": datetime.utcnow().isoformat()
    }
    supabase.table("pagos").update(data).eq("id", pago_id).execute()

def pagos_get(pago_id: str):
    r = supabase.table("pagos").select("*").eq("id", pago_id).limit(1).execute()
    return r.data[0] if r.data else None

def user_add_credits(user_id: str, cuentas: int):
    """Cada cuenta = COSTO_CUENTA soles = 1 cuenta asignada"""
    r = supabase.table("usuarios").select("creditos").eq("telegram_id", str(user_id)).limit(1).execute()
    current = int(r.data[0]["creditos"]) if r.data and r.data[0].get("creditos") is not None else 0
    new_value = current + cuentas
    supabase.table("usuarios").update({"creditos": new_value}).eq("telegram_id", str(user_id)).execute()
    return cuentas, new_value


# -------------------- QR --------------------
def build_qr_png_bytes(text: str) -> bytes:
    img = qrcode.make(text)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# -------------------- TELEGRAM --------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Bienvenido.\n"
        f"Tarifa: {COSTO_CUENTA:.2f} soles por cuenta\n\n"
        "Usa /recargar <número_cuentas>\n\n"
        "Ejemplo:\n"
        "/recargar 1  → 25 soles\n"
        "/recargar 2  → 50 soles"
    )

async def cmd_recargar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if len(context.args) != 1:
        await update.message.reply_text("Uso correcto: /recargar <número_cuentas>\nEj: /recargar 2")
        return
    try:
        cuentas = int(context.args[0])
        if cuentas <= 0:
            raise ValueError()
    except Exception:
        await update.message.reply_text("❌ Número inválido. Ej: /recargar 1")
        return

    total = cuentas * COSTO_CUENTA
    pago_id = str(uuid.uuid4())
    pagos_upsert(pago_id, str(user.id), user.username or "", cuentas, total)

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📸 Enviar comprobante Yape", callback_data=f"yape:{pago_id}")]
    ])

    await update.message.reply_text(
        f"🧾 Pedido generado\n"
        f"👉 <b>{cuentas}</b> cuenta(s)\n"
        f"💵 Total: <b>{total:.2f} {CURRENCY}</b>\n"
        f"📌 ID: <code>{pago_id}</code>\n\n"
        "🔹 Paga con Yape al número: <b>999999999</b>\n"
        "Luego envía el comprobante.",
        parse_mode="HTML",
        reply_markup=kb
    )

    # 🔔 Notificar al admin
    await tg_app.bot.send_message(
        chat_id=ADMIN_ID,
        text=(
            f"📥 Nueva solicitud de {user.username or user.id}\n"
            f"ID: {pago_id}\n"
            f"Cuentas: {cuentas}\n"
            f"Monto: {total:.2f} {CURRENCY}\n\n"
            "Verifica el pago en Yape y usa /historial para gestionarlo."
        )
    )

# -------------------- HISTORIAL (admin) --------------------
async def cmd_historial(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != ADMIN_ID:
        await update.message.reply_text("⛔ No tienes permiso para usar este comando.")
        return

    r = supabase.table("pagos").select("*").order("created_at", desc=True).limit(10).execute()
    rows = r.data or []
    if not rows:
        await update.message.reply_text("ℹ️ No hay solicitudes registradas.")
        return

    for row in rows:
        text = (
            f"🆔 <code>{row['id']}</code>\n"
            f"👤 Usuario: {row.get('username') or '-'} (ID {row['user_id']})\n"
            f"💵 Monto: {row['amount']} {CURRENCY}\n"
            f"📌 Estado: <b>{row['status']}</b>\n"
            f"📅 Fecha: {row['created_at']}"
        )

        if row["status"] == "pendiente":
            kb = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Aprobar", callback_data=f"aprobar:{row['id']}"),
                    InlineKeyboardButton("❌ Rechazar", callback_data=f"rechazar:{row['id']}")
                ]
            ])
            await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)
        else:
            await update.message.reply_text(text, parse_mode="HTML")

# -------------------- CALLBACKS (admin aprueba/rechaza) --------------------
async def on_admin_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    action, pago_id = data.split(":", 1)

    row = pagos_get(pago_id)
    if not row:
        await q.message.reply_text("❌ Pedido no encontrado.")
        return

    if action == "aprobar":
        pagos_set_status(pago_id, "aprobado", ADMIN_ID)
        cuentas, total = user_add_credits(row["user_id"], row["cuentas"])
        await tg_app.bot.send_message(
            chat_id=row["user_id"],
            text=f"✅ Pago confirmado.\nSe añadieron <b>{cuentas}</b> créditos.\nSaldo actualizado: <b>{total}</b>",
            parse_mode="HTML"
        )
        await q.message.edit_text(f"✅ Pedido {pago_id} aprobado.")
    elif action == "rechazar":
        pagos_set_status(pago_id, "rechazado", ADMIN_ID)
        await tg_app.bot.send_message(
            chat_id=row["user_id"],
            text="❌ Tu pago fue rechazado. Contacta con soporte."
        )
        await q.message.edit_text(f"❌ Pedido {pago_id} rechazado.")

# -------------------- REGISTRO HANDLERS --------------------
tg_app.add_handler(CommandHandler("start", cmd_start))
tg_app.add_handler(CommandHandler("recargar", cmd_recargar))
tg_app.add_handler(CommandHandler("historial", cmd_historial))
tg_app.add_handler(CallbackQueryHandler(on_admin_action, pattern=r"^(aprobar|rechazar):"))

# -------------------- MAIN --------------------
def create_app():
    def _start_polling():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        tg_app.run_polling(close_loop=True)

    threading.Thread(target=_start_polling, daemon=True).start()
    return app_flask

if __name__ == "__main__":
    from waitress import serve
    port = int(os.getenv("PORT", "8080"))
    threading.Thread(target=lambda: tg_app.run_polling(close_loop=True), daemon=True).start()
    serve(app_flask, host="0.0.0.0", port=port)
