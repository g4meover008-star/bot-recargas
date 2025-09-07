import os
import io
import uuid
import asyncio
import logging
import threading
from datetime import datetime

import qrcode
from flask import Flask, request, jsonify
from mercadopago import SDK
from supabase import create_client, Client

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, CallbackQueryHandler

# -------------------- LOGGING --------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("recargas")

# -------------------- ENV --------------------
def _env(name: str, default: str = "") -> str:
    # Limpia espacios, comillas y saltos de línea invisibles
    val = os.getenv(name, default) or ""
    val = val.strip().strip('"').strip("'").replace("\r", "").replace("\n", "")
    return val

TG_BOT_TOKEN   = _env("TG_RECHARGE_BOT_TOKEN")
SUPABASE_URL   = _env("SUPABASE_URL")
SUPABASE_KEY   = _env("SUPABASE_ANON_KEY") or _env("SUPABASE_API_KEY")
MP_ACCESS_TOKEN = _env("MP_ACCESS_TOKEN")
PUBLIC_BASE_URL = _env("PUBLIC_BASE_URL").rstrip("/")
CURRENCY       = _env("CURRENCY", "PEN")
PRICE_PER_CREDIT = float(_env("PRICE_PER_CREDIT", "1"))

if not (TG_BOT_TOKEN and SUPABASE_URL and SUPABASE_KEY and MP_ACCESS_TOKEN and PUBLIC_BASE_URL):
    raise SystemExit(
        "Faltan variables de entorno: TG_RECHARGE_BOT_TOKEN, SUPABASE_URL, "
        "SUPABASE_ANON_KEY/SUPABASE_API_KEY, MP_ACCESS_TOKEN, PUBLIC_BASE_URL"
    )

# Evita proxies heredados (a veces rompen httpx/gotrue/telegram)
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
    os.environ.pop(_k, None)

# Log útil (enmascarado) para confirmar que el token quedó limpio
log.info("Token TG len=%s, termina en ...%s", len(TG_BOT_TOKEN), TG_BOT_TOKEN[-6:])
log.info("PUBLIC_BASE_URL=%s", PUBLIC_BASE_URL)

# -------------------- CLIENTES --------------------
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
mp = SDK(MP_ACCESS_TOKEN)

# Flask y Telegram
app_flask = Flask(__name__)
tg_app = ApplicationBuilder().token(TG_BOT_TOKEN).build()

# -------------------- DB helpers --------------------
def pagos_upsert(pago_id: str, user_id: str, username: str, amount: float, pref_id: str, init_point: str):
    supabase.table("pagos").upsert(
        {
            "id": pago_id,
            "user_id": str(user_id),
            "username": username,
            "amount": amount,
            "status": "pendiente",
            "preference_id": pref_id,
            "init_point": init_point,
            "created_at": datetime.utcnow().isoformat(),
        },
        on_conflict="id",
    ).execute()

def pagos_set_status(pago_id: str, new_status: str, payment_id: str | None = None):
    data = {"status": new_status, "updated_at": datetime.utcnow().isoformat()}
    if payment_id:
        data["payment_id"] = str(payment_id)
    supabase.table("pagos").update(data).eq("id", pago_id).execute()

def pagos_get(pago_id: str):
    r = supabase.table("pagos").select("*").eq("id", pago_id).limit(1).execute()
    return r.data[0] if r.data else None

def user_add_credits(user_id: str, amount_paid: float):
    """1 sol = 1 crédito (ajustable con PRICE_PER_CREDIT)."""
    r = supabase.table("usuarios").select("creditos").eq("telegram_id", str(user_id)).limit(1).execute()
    current = int(r.data[0]["creditos"]) if r.data and r.data[0].get("creditos") is not None else 0
    to_add = int(round(amount_paid / PRICE_PER_CREDIT))
    new_value = current + to_add
    supabase.table("usuarios").update({"creditos": new_value}).eq("telegram_id", str(user_id)).execute()
    # Historial opcional
    try:
        supabase.table("creditos_historial").insert(
            {"usuario_id": str(user_id), "delta": to_add, "motivo": "recarga_mp", "hecho_por": "mercado_pago"}
        ).execute()
    except Exception:
        pass
    return to_add, new_value

# -------------------- MP helpers --------------------
def mp_create_preference(pago_id: str, amount: float):
    pref = {
        "items": [
            {
                "title": "Recarga de créditos",
                "quantity": 1,
                "currency_id": CURRENCY,
                "unit_price": float(amount),
            }
        ],
        "external_reference": pago_id,
        "notification_url": f"{PUBLIC_BASE_URL}/mp/webhook",
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
        "🤖 Bot de Recargas\n"
        "Usa /recargar <monto>\n\n"
        "Ejemplos:\n"
        "/recargar 5  (5 soles)\n"
        "/recargar 10 (10 soles)\n\n"
        "Te daré un link de pago (y QR). Cuando MP apruebe, acreditaré créditos."
    )

async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong ✅")

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
    pref_id, init_point = mp_create_preference(pago_id, amount)
    pagos_upsert(pago_id, str(user.id), user.username or "", amount, pref_id, init_point)

    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("💳 Pagar ahora", url=init_point)],
            [InlineKeyboardButton("🧾 Ver QR", callback_data=f"qr:{pago_id}")],
        ]
    )
    await update.message.reply_text(
        f"🔗 Link de pago por {amount:.2f} {CURRENCY}\n"
        f"Pedido: <code>{pago_id}</code>\n\n"
        "Si prefieres QR, pulsa “Ver QR”.",
        reply_markup=kb,
    )

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

tg_app.add_handler(CommandHandler("start", cmd_start))
tg_app.add_handler(CommandHandler("ping", cmd_ping))
tg_app.add_handler(CommandHandler("recargar", cmd_recargar))
tg_app.add_handler(CallbackQueryHandler(on_qr_callback, pattern=r"^qr:"))

# -------------------- FLASK --------------------
@app_flask.get("/health")
def health():
    return "ok", 200

@app_flask.post("/mp/webhook")
def mp_webhook():
    try:
        body = request.get_json(force=True, silent=True) or {}
        log.info(f"Webhook MP: {body}")

        payment_id = None
        if body.get("type") == "payment" and body.get("data", {}).get("id"):
            payment_id = str(body["data"]["id"])
        elif body.get("data", {}).get("id"):
            payment_id = str(body["data"]["id"])

        if not payment_id:
            return jsonify({"status": "ignored"}), 200

        p = mp_get_payment(payment_id)
        resp = p.get("response", {})
        status = resp.get("status")
        ext_ref = resp.get("external_reference")
        amount = float(resp.get("transaction_amount") or 0.0)

        if not ext_ref:
            return jsonify({"error": "sin external_reference"}), 200

        pedido = pagos_get(ext_ref)
        if not pedido:
            return jsonify({"status": "unknown order"}), 200

        # Idempotencia
        if pedido.get("status") == "aprobado":
            return jsonify({"status": "already processed"}), 200

        if status == "approved":
            pagos_set_status(ext_ref, "aprobado", payment_id)
            user_id = pedido["user_id"]
            added, new_total = user_add_credits(user_id, amount)
            # Notifica por Telegram (no bloquea)
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                loop = None
            if loop and loop.is_running():
                loop.create_task(notify_user(int(user_id), amount, added, new_total))
            else:
                # fallback: lanza un hilo con su propio loop
                def _notify():
                    ll = asyncio.new_event_loop()
                    asyncio.set_event_loop(ll)
                    ll.run_until_complete(notify_user(int(user_id), amount, added, new_total))
                    ll.close()
                threading.Thread(target=_notify, daemon=True).start()

            return jsonify({"status": "ok"}), 200

        elif status in ("rejected", "cancelled", "refunded", "charged_back"):
            pagos_set_status(ext_ref, status, payment_id)
            return jsonify({"status": status}), 200
        else:
            pagos_set_status(ext_ref, status, payment_id)
            return jsonify({"status": status}), 200

    except Exception as e:
        log.exception("Error en webhook")
        return jsonify({"error": str(e)}), 500

async def notify_user(user_id: int, amount_paid: float, added: int, new_total: int):
    try:
        text = (
            "✅ <b>Recarga acreditada</b>\n"
            f"Pago: {amount_paid:.2f} {CURRENCY}\n"
            f"Créditos añadidos: <b>{added}</b>\n"
            f"Saldo actual: <b>{new_total}</b>"
        )
        await tg_app.bot.send_message(chat_id=user_id, text=text, parse_mode="HTML")
    except Exception:
        log.warning("No pude notificar al usuario.")

# -------------------- FACTORY PARA GUNICORN --------------------
def create_app():
    """
    Usado por Gunicorn. Arranca Telegram en un hilo aparte
    y devuelve la app Flask para Railway.
    """
    def _start_telegram_polling():
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            log.info("Iniciando polling de Telegram…")
            # EVITA set_wakeup_fd en hilo
            tg_app.run_polling(close_loop=True, stop_signals=())
        except Exception:
            log.exception("Fallo al iniciar el polling de Telegram")

    threading.Thread(target=_start_telegram_polling, name="tg-polling", daemon=True).start()
    return app_flask

# -------------------- MAIN (ejecución local) --------------------
if __name__ == "__main__":
    from waitress import serve
    port = int(os.getenv("PORT", "8080"))

    def _start_telegram_polling_local():
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            log.info("Iniciando polling de Telegram (local)…")
            # EVITA set_wakeup_fd en hilo
            tg_app.run_polling(close_loop=True, stop_signals=())
        except Exception:
            log.exception("Fallo al iniciar el polling de Telegram")

    threading.Thread(target=_start_telegram_polling_local, name="tg-polling", daemon=True).start()

    log.info(f"Sirviendo Flask en 0.0.0.0:{port}")
    serve(app_flask, host="0.0.0.0", port=port)
