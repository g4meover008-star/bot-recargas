# recharge_bot.py

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
SUPABASE_KEY = os.getenv("SUPABASE_ANON_KEY") or os.getenv("SUPABASE_API_KEY") or ""
MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN", "")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "")   # ej: https://web-production-xxxx.up.railway.app
CURRENCY = os.getenv("CURRENCY", "PEN")
PRICE_PER_CREDIT = float(os.getenv("PRICE_PER_CREDIT", "25"))  # 25 por cuenta por defecto
BRAND_NAME = os.getenv("BRAND_NAME", "Bot de Recargas")

if not (TG_BOT_TOKEN and SUPABASE_URL and SUPABASE_KEY and MP_ACCESS_TOKEN and PUBLIC_BASE_URL):
    raise SystemExit(
        "Faltan variables de entorno: TG_RECHARGE_BOT_TOKEN, SUPABASE_URL, "
        "SUPABASE_ANON_KEY/SUPABASE_API_KEY, MP_ACCESS_TOKEN, PUBLIC_BASE_URL"
    )

#  Evita que gotrue/httpx recojan proxies del entorno
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
    os.environ.pop(_k, None)

# -------------------- CLIENTES --------------------
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
mp = SDK(MP_ACCESS_TOKEN)
app_flask = Flask(__name__)
tg_app = ApplicationBuilder().token(TG_BOT_TOKEN).build()


# -------------------- DB helpers --------------------
def pagos_upsert(pago_id: str, user_id: str, username: str, amount: float, pref_id: str, init_point: str, unidades: int):
    supabase.table("pagos").upsert(
        {
            "id": pago_id,
            "user_id": str(user_id),
            "username": username,
            "amount": amount,                # total en dinero
            "units": unidades,               # número de cuentas/creditos solicitados
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


def user_add_credits(user_id: str, credits_to_add: int):
    r = supabase.table("usuarios").select("creditos").eq("telegram_id", str(user_id)).limit(1).execute()
    current = int(r.data[0]["creditos"]) if r.data and r.data[0].get("creditos") is not None else 0
    new_value = current + credits_to_add
    supabase.table("usuarios").update({"creditos": new_value}).eq("telegram_id", str(user_id)).execute()
    # Historial (opcional)
    try:
        supabase.table("creditos_historial").insert(
            {"usuario_id": str(user_id), "delta": credits_to_add, "motivo": "recarga_mp", "hecho_por": "mercado_pago"}
        ).execute()
    except Exception:
        pass
    return new_value


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
def _welcome_text() -> str:
    return (
        f"👋 Bienvenido a <b>{BRAND_NAME}</b>\n\n"
        f"Tarifa: <b>{PRICE_PER_CREDIT:.2f} {CURRENCY}</b> por crédito.\n\n"
        "Usa <code>/recargar &lt;montos_en_cuentas&gt;</code>\n"
        "Ejemplos:\n"
        f"<code>/recargar 5</code>  (5 cuentas → {5*PRICE_PER_CREDIT:.2f} {CURRENCY})\n"
        f"<code>/recargar 10</code> (10 cuentas → {10*PRICE_PER_CREDIT:.2f} {CURRENCY})\n\n"
        "Te daré un link de pago (y QR). Cuando MP apruebe, acreditaré créditos."
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_html(_welcome_text())


async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong ✅")


async def cmd_recargar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if len(context.args) != 1:
        await update.message.reply_html(
            "Uso: <code>/recargar &lt;numero_de_cuentas&gt;</code>\n\n" + _welcome_text()
        )
        return

    try:
        unidades = int(context.args[0])
        if unidades <= 0:
            raise ValueError()
    except Exception:
        await update.message.reply_text("Monto inválido. Ej: /recargar 5")
        return

    total = round(unidades * PRICE_PER_CREDIT, 2)

    pago_id = str(uuid.uuid4())
    pref_id, init_point = mp_create_preference(pago_id, total)
    pagos_upsert(pago_id, str(user.id), user.username or "", total, pref_id, init_point, unidades)

    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("💳 Pagar ahora", url=init_point)],
            [InlineKeyboardButton("🧾 Ver QR", callback_data=f"qr:{pago_id}")],
        ]
    )

    await update.message.reply_html(
        f"🔢 Cuentas solicitadas: <b>{unidades}</b>\n"
        f"💰 Total a pagar: <b>{total:.2f} {CURRENCY}</b>\n"
        f"🆔 Pedido: <code>{pago_id}</code>\n\n"
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
tg_app.add_handler(CommandHandler("recargar", cmd_recargar)))
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
            unidades = int(pedido.get("units") or 0)
            if unidades <= 0:
                # Fallback: calcula por dinero/Precio
                unidades = int(round(float(pedido.get("amount") or 0) / PRICE_PER_CREDIT))

            new_total = user_add_credits(user_id, unidades)

            # Notifica por Telegram (no bloquea)
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                loop = None

            async def _notify():
                try:
                    txt = (
                        "✅ <b>Recarga acreditada</b>\n"
                        f"Créditos añadidos: <b>{unidades}</b>\n"
                        f"Saldo actual: <b>{new_total}</b>"
                    )
                    await tg_app.bot.send_message(chat_id=int(user_id), text=txt, parse_mode="HTML")
                except Exception:
                    log.warning("No pude notificar al usuario.")

            if loop and loop.is_running():
                loop.create_task(_notify())
            else:
                def _t():
                    ll = asyncio.new_event_loop()
                    asyncio.set_event_loop(ll)
                    ll.run_until_complete(_notify())
                    ll.close()
                threading.Thread(target=_t, daemon=True).start()

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
            # IMPORTANTE: evitar señales en hilo secundario
            tg_app.run_polling(
                close_loop=True,
                stop_signals=None,
                allowed_updates=Update.ALL_TYPES,
            )
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
            tg_app.run_polling(
                close_loop=True,
                stop_signals=None,
                allowed_updates=Update.ALL_TYPES,
            )
        except Exception:
            log.exception("Fallo al iniciar el polling de Telegram")

    threading.Thread(target=_start_telegram_polling_local, name="tg-polling", daemon=True).start()

    log.info(f"Sirviendo Flask en 0.0.0.0:{port}")
    serve(app_flask, host="0.0.0.0", port=port)
