# recharge_bot.py
import os
import io
import uuid
import asyncio
import logging
import threading
from datetime import datetime
from typing import Optional

import qrcode
from flask import Flask, request, jsonify
from mercadopago import SDK
from supabase import create_client, Client
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Bot as TgBot,
)
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, CallbackQueryHandler


# -------------------- LOGGING --------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("recargas")


# -------------------- ENV --------------------
def _env(name: str, default: str = "") -> str:
    v = os.getenv(name, default)
    return v.strip() if isinstance(v, str) else default


TG_BOT_TOKEN = _env("TG_RECHARGE_BOT_TOKEN")
SUPABASE_URL = _env("SUPABASE_URL")
SUPABASE_KEY = _env("SUPABASE_API_KEY") or _env("SUPABASE_ANON_KEY")
MP_ACCESS_TOKEN = _env("MP_ACCESS_TOKEN")
PUBLIC_BASE_URL = _env("PUBLIC_BASE_URL")  # ej: https://web-production-xxxx.up.railway.app

CURRENCY = _env("CURRENCY", "PEN")
PRICE_PER_CREDIT = float(_env("PRICE_PER_CREDIT", "1"))

# Opcional (para notificar al bot principal / admin)
ADMIN_BOT_TOKEN = _env("ADMIN_BOT_TOKEN")   # token del bot que recibirá las notificaciones
ADMIN_CHAT_ID = _env("ADMIN_CHAT_ID")       # chat_id (numérico) donde avisar
BRAND_NAME = _env("BRAND_NAME", "Bot de Recargas")

if not (TG_BOT_TOKEN and SUPABASE_URL and SUPABASE_KEY and MP_ACCESS_TOKEN and PUBLIC_BASE_URL):
    raise SystemExit(
        "Faltan variables de entorno: TG_RECHARGE_BOT_TOKEN, SUPABASE_URL, "
        "SUPABASE_API_KEY/SUPABASE_ANON_KEY, MP_ACCESS_TOKEN, PUBLIC_BASE_URL"
    )

# Por si el entorno trae proxies heredados que rompen httpx/requests
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
    os.environ.pop(_k, None)


# -------------------- CLIENTES --------------------
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)  # <- SIN 'options', así funciona bien
mp = SDK(MP_ACCESS_TOKEN)
app_flask = Flask(__name__)
tg_app = ApplicationBuilder().token(TG_BOT_TOKEN).build()

_admin_bot: Optional[TgBot] = None
if ADMIN_BOT_TOKEN and ADMIN_CHAT_ID:
    try:
        _admin_bot = TgBot(ADMIN_BOT_TOKEN)
        log.info("Admin bot configurado para notificaciones.")
    except Exception:
        log.exception("No pude inicializar el admin bot.")


# -------------------- HELPERS DB --------------------
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


def pagos_set_status(pago_id: str, new_status: str, payment_id: Optional[str] = None):
    data = {"status": new_status, "updated_at": datetime.utcnow().isoformat()}
    if payment_id:
        data["payment_id"] = str(payment_id)
    supabase.table("pagos").update(data).eq("id", pago_id).execute()


def pagos_get(pago_id: str):
    r = supabase.table("pagos").select("*").eq("id", pago_id).limit(1).execute()
    return r.data[0] if r.data else None


def user_add_credits(user_id: str, amount_paid: float):
    r = supabase.table("usuarios").select("creditos").eq("telegram_id", str(user_id)).limit(1).execute()
    current = int(r.data[0]["creditos"]) if r.data and r.data[0].get("creditos") is not None else 0
    to_add = int(round(amount_paid / PRICE_PER_CREDIT))
    new_value = current + to_add
    supabase.table("usuarios").update({"creditos": new_value}).eq("telegram_id", str(user_id)).execute()
    try:
        supabase.table("creditos_historial").insert(
            {
                "usuario_id": str(user_id),
                "delta": to_add,
                "motivo": "recarga_mp",
                "hecho_por": "mercado_pago",
            }
        ).execute()
    except Exception:
        pass
    return to_add, new_value


# -------------------- HELPERS MP --------------------
def mp_create_preference(pago_id: str, amount: float):
    pref = {
        "items": [
            {
                "title": f"{BRAND_NAME} - Recarga de créditos",
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


# -------------------- NOTIFICACIONES --------------------
async def notify_user(user_id: int, amount_paid: float, added: int, new_total: int):
    try:
        text = (
            f"✅ <b>Recarga acreditada</b>\n"
            f"Monto: {amount_paid:.2f} {CURRENCY}\n"
            f"Créditos añadidos: <b>{added}</b>\n"
            f"Saldo actual: <b>{new_total}</b>"
        )
        await tg_app.bot.send_message(chat_id=user_id, text=text, parse_mode="HTML")
    except Exception:
        log.warning("No pude notificar al usuario.")


def notify_admin_sync(text: str):
    if not (_admin_bot and ADMIN_CHAT_ID):
        return
    try:
        _admin_bot.send_message(chat_id=int(ADMIN_CHAT_ID), text=text, parse_mode="HTML", disable_web_page_preview=True)
    except Exception:
        log.warning("No pude notificar al admin.")


# -------------------- TELEGRAM CMDS --------------------
WELCOME = (
    f"👋 Bienvenido a <b>{BRAND_NAME}</b>\n\n"
    f"Tarifa: <b>{PRICE_PER_CREDIT:.2f} {CURRENCY}</b> por crédito.\n\n"
    "Usa <code>/recargar &lt;monto&gt;</code>\n"
    "Ejemplos:\n"
    "<code>/recargar 5</code> (5 soles)\n"
    "<code>/recargar 10</code> (10 soles)\n\n"
    "Te daré un link de pago (y QR). Cuando MP apruebe, acreditaré créditos."
)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME, parse_mode="HTML")


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
        parse_mode="HTML",
    )

    # Aviso al admin (opcional)
    notify_admin_sync(
        f"🆕 <b>Solicitud de recarga</b>\n"
        f"Usuario: <code>{user.id}</code> @{user.username or '-'}\n"
        f"Monto: <b>{amount:.2f} {CURRENCY}</b>\n"
        f"Pedido: <code>{pago_id}</code>\n"
        f"Link: {init_point}"
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


# -------------------- FLASK ROUTES --------------------
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
            user_id = int(pedido["user_id"])
            added, new_total = user_add_credits(str(user_id), amount)

            # Notifica al usuario (asincrónico, sin bloquear Flask)
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                loop = None

            if loop and loop.is_running():
                loop.create_task(notify_user(user_id, amount, added, new_total))
            else:
                def _notify():
                    ll = asyncio.new_event_loop()
                    asyncio.set_event_loop(ll)
                    ll.run_until_complete(notify_user(user_id, amount, added, new_total))
                    ll.close()
                threading.Thread(target=_notify, daemon=True).start()

            # Aviso al admin
            notify_admin_sync(
                f"✅ <b>Pago aprobado</b>\n"
                f"User: <code>{user_id}</code>\n"
                f"Monto: {amount:.2f} {CURRENCY}\n"
                f"Créditos +{added} → saldo {new_total}\n"
                f"Pedido: <code>{ext_ref}</code>\n"
                f"PaymentID: <code>{payment_id}</code>"
            )

            return jsonify({"status": "ok"}), 200

        else:
            pagos_set_status(ext_ref, status or "unknown", payment_id)
            # Aviso al admin de estados no-aprobados
            notify_admin_sync(
                f"ℹ️ <b>Estado de pago</b>: {status}\n"
                f"Pedido: <code>{ext_ref}</code>\n"
                f"PaymentID: <code>{payment_id}</code>"
            )
            return jsonify({"status": status}), 200

    except Exception as e:
        log.exception("Error en webhook")
        return jsonify({"error": str(e)}), 500


# -------------------- FACTORY PARA GUNICORN --------------------
def create_app():
    """
    Usado por Gunicorn. Arranca Telegram en un hilo y devuelve la app Flask.
    Evitamos señales en threads con stop_signals=None.
    """
    def _start_telegram_polling():
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            log.info("Iniciando polling de Telegram…")
            tg_app.run_polling(close_loop=True, stop_signals=None)
        except Exception:
            log.exception("Fallo al iniciar el polling de Telegram")

    threading.Thread(target=_start_telegram_polling, name="tg-polling", daemon=True).start()
    return app_flask


# -------------------- MAIN: ejecución local --------------------
if __name__ == "__main__":
    from waitress import serve
    port = int(os.getenv("PORT", "8080"))

    def _start_telegram_polling_local():
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            log.info("Iniciando polling de Telegram (local)…")
            tg_app.run_polling(close_loop=True, stop_signals=None)
        except Exception:
            log.exception("Fallo al iniciar el polling de Telegram")

    threading.Thread(target=_start_telegram_polling_local, name="tg-polling", daemon=True).start()

    log.info(f"Sirviendo Flask en 0.0.0.0:{port}")
    serve(app_flask, host="0.0.0.0", port=port)
