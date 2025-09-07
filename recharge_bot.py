import os
import io
import uuid
import asyncio
import logging
import threading
from datetime import datetime

import httpx
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
TG_BOT_TOKEN = os.getenv("TG_RECHARGE_BOT_TOKEN", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_ANON_KEY") or os.getenv("SUPABASE_API_KEY") or ""
MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN", "")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "")  # ej: https://web-production-xxxx.up.railway.app

CURRENCY = os.getenv("CURRENCY", "PEN")
PRICE_PER_CREDIT = float(os.getenv("PRICE_PER_CREDIT", "1"))
PRICE_PER_ACCOUNT = float(os.getenv("PRICE_PER_ACCOUNT", "25"))  # <<< nuevo
BRAND_NAME = os.getenv("BRAND_NAME", "Netflix_M4CRO")           # <<< opcional

# Notificación al bot principal
ADMIN_BOT_TOKEN = os.getenv("ADMIN_BOT_TOKEN", "")              # <<< nuevo (token del bot principal)
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "")                  # <<< nuevo (chat_id donde avisa)

if not (TG_BOT_TOKEN and SUPABASE_URL and SUPABASE_KEY and MP_ACCESS_TOKEN and PUBLIC_BASE_URL):
    raise SystemExit(
        "Faltan variables de entorno: TG_RECHARGE_BOT_TOKEN, SUPABASE_URL, "
        "SUPABASE_ANON_KEY/SUPABASE_API_KEY, MP_ACCESS_TOKEN, PUBLIC_BASE_URL"
    )

# Limpia proxies heredados (evita 'unexpected keyword argument proxy' en httpx/gotrue)
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
    os.environ.pop(_k, None)

# -------------------- CLIENTES --------------------
_httpx = httpx.Client(timeout=30.0)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY, options={"http_client": _httpx})
mp = SDK(MP_ACCESS_TOKEN)
app_flask = Flask(__name__)

# Telegram app global
tg_app = ApplicationBuilder().token(TG_BOT_TOKEN).build()

# -------------------- HELPERS --------------------
def build_qr_png_bytes(url: str) -> bytes:
    img = qrcode.make(url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()

def pesos(amount: float) -> str:
    return f"{amount:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

def notify_admin_sync(text: str) -> None:
    """Usa el BOT PRINCIPAL (ADMIN_BOT_TOKEN) para mandar mensajes a ADMIN_CHAT_ID."""
    if not (ADMIN_BOT_TOKEN and ADMIN_CHAT_ID):
        return
    try:
        api = f"https://api.telegram.org/bot{ADMIN_BOT_TOKEN}/sendMessage"
        _httpx.post(api, json={"chat_id": ADMIN_CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=20)
    except Exception:
        log.warning("No pude notificar al admin.", exc_info=True)

def notify_admin_bg(text: str) -> None:
    threading.Thread(target=notify_admin_sync, args=(text,), daemon=True).start()

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
    r = supabase.table("usuarios").select("creditos").eq("telegram_id", str(user_id)).limit(1).execute()
    current = int(r.data[0]["creditos"]) if r.data and r.data[0].get("creditos") is not None else 0
    to_add = int(round(amount_paid / PRICE_PER_CREDIT))
    new_value = current + to_add
    supabase.table("usuarios").update({"creditos": new_value}).eq("telegram_id", str(user_id)).execute()
    try:
        supabase.table("creditos_historial").insert(
            {"usuario_id": str(user_id), "delta": to_add, "motivo": "recarga_mp", "hecho_por": "mercado_pago"}
        ).execute()
    except Exception:
        pass
    return to_add, new_value

def compras_insert(order_id: str, user_id: str, username: str, qty: int, unit_price: float, total: float):
    """Tabla opcional 'compras' para registrar la intención de compra (manual)."""
    try:
        supabase.table("compras").insert(
            {
                "id": order_id,
                "user_id": str(user_id),
                "username": username,
                "cantidad": qty,
                "precio_unit": unit_price,
                "total": total,
                "status": "pendiente",
                "created_at": datetime.utcnow().isoformat(),
            }
        ).execute()
    except Exception:
        pass

# -------------------- Mercado Pago helpers --------------------
def mp_create_preference(pago_id: str, amount: float):
    pref = {
        "items": [
            {"title": "Recarga de créditos", "quantity": 1, "currency_id": CURRENCY, "unit_price": float(amount)}
        ],
        "external_reference": pago_id,
        "notification_url": f"{PUBLIC_BASE_URL}/mp/webhook",
    }
    resp = mp.preference().create(pref)
    return resp["response"]["id"], resp["response"]["init_point"]

def mp_get_payment(payment_id: str):
    return mp.payment().get(payment_id)

# -------------------- TELEGRAM CMDS --------------------
WELCOME_TEXT = (
    f"👋 Bienvenido a <b>{BRAND_NAME}</b>.\n"
    f"Tarifa: <b>{pesos(PRICE_PER_ACCOUNT)} {CURRENCY}</b> por cuenta.\n\n"
    "Selecciona una opción:\n"
    "• <code>/comprar 1</code> (o la cantidad que desees)\n"
    "• <code>/recargar 10</code> (recarga de créditos por Mercado Pago)\n"
    "• <code>/ping</code> (prueba rápida)"
)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("Créditos", callback_data="menu:creditos")]])
    await update.message.reply_text(WELCOME_TEXT, parse_mode="HTML", reply_markup=kb)

async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong ✅")

async def on_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "menu:creditos":
        await q.message.reply_text("Usa /recargar <monto> para recargar créditos. Ej: /recargar 10")

async def cmd_comprar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Uso: /comprar <cantidad>"""
    user = update.effective_user
    if len(context.args) != 1:
        await update.message.reply_text("Uso: /comprar <cantidad>\nEj: /comprar 1")
        return
    try:
        qty = int(context.args[0])
        if qty <= 0:
            raise ValueError()
    except Exception:
        await update.message.reply_text("Cantidad inválida. Ej: /comprar 1")
        return

    total = PRICE_PER_ACCOUNT * qty
    order_id = str(uuid.uuid4())

    # Opcional: registra la intención
    compras_insert(order_id, str(user.id), user.username or "", qty, PRICE_PER_ACCOUNT, total)

    # Mensaje al usuario (estilo ejemplo)
    text_user = (
        f"Ok, el precio por cada cuenta es <b>{pesos(PRICE_PER_ACCOUNT)} {CURRENCY}</b>.\n"
        f"El monto total a enviar es <b>{pesos(total)} {CURRENCY}</b>.\n\n"
        "Cuando completes el pago, envía el <b>comprobante</b> por aquí para validarlo. "
        "Si tienes dudas, escribe /start."
    )
    await update.message.reply_text(text_user, parse_mode="HTML")

    # Notifica al admin por el BOT PRINCIPAL
    username = f"@{user.username}" if user.username else f"(id {user.id})"
    admin_msg = (
        f"🆕 <b>Nueva solicitud de compra</b>\n"
        f"Usuario: {username}\n"
        f"Order ID: <code>{order_id}</code>\n"
        f"Cantidad: <b>{qty}</b>\n"
        f"Unit: <b>{pesos(PRICE_PER_ACCOUNT)} {CURRENCY}</b>\n"
        f"Total: <b>{pesos(total)} {CURRENCY}</b>"
    )
    notify_admin_bg(admin_msg)

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
        f"🔗 Link de pago por {pesos(amount)} {CURRENCY}\n"
        f"Pedido: <code>{pago_id}</code>\n\n"
        "Si prefieres QR, pulsa “Ver QR”.",
        reply_markup=kb,
        parse_mode="HTML",
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
tg_app.add_handler(CommandHandler("comprar", cmd_comprar))
tg_app.add_handler(CommandHandler("recargar", cmd_recargar))
tg_app.add_handler(CallbackQueryHandler(on_qr_callback, pattern=r"^qr:"))
tg_app.add_handler(CallbackQueryHandler(on_menu, pattern=r"^menu:"))

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

            # Notifica al usuario (bot de recargas)
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                loop = None
            if loop and loop.is_running():
                loop.create_task(notify_user(int(user_id), amount, added, new_total))
            else:
                def _notify():
                    ll = asyncio.new_event_loop()
                    asyncio.set_event_loop(ll)
                    ll.run_until_complete(notify_user(int(user_id), amount, added, new_total))
                    ll.close()
                threading.Thread(target=_notify, daemon=True).start()

            # Notifica al admin (bot principal)
            notify_admin_bg(
                f"✅ <b>Recarga acreditada</b>\n"
                f"User ID: <code>{user_id}</code>\n"
                f"Monto: <b>{pesos(amount)} {CURRENCY}</b>\n"
                f"Créditos añadidos: <b>{added}</b>\n"
                f"Pedido: <code>{ext_ref}</code>"
            )

            return jsonify({"status": "ok"}), 200

        elif status in ("rejected", "cancelled", "refunded", "charged_back"):
            pagos_set_status(ext_ref, status, payment_id)
            notify_admin_bg(
                f"⚠️ Estado de pago: <b>{status}</b>\nPedido: <code>{ext_ref}</code>"
            )
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
            f"Pago: {pesos(amount_paid)} {CURRENCY}\n"
            f"Créditos añadidos: <b>{added}</b>\n"
            f"Saldo actual: <b>{new_total}</b>"
        )
        await tg_app.bot.send_message(chat_id=user_id, text=text, parse_mode="HTML")
    except Exception:
        log.warning("No pude notificar al usuario.")

# -------------------- FACTORY PARA GUNICORN --------------------
def create_app():
    """Usado por Gunicorn. Arranca Telegram en un hilo aparte y devuelve Flask."""
    def _start_telegram_polling():
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            log.info("Iniciando polling de Telegram…")
            tg_app.run_polling(close_loop=True)
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
            tg_app.run_polling(close_loop=True)
        except Exception:
            log.exception("Fallo al iniciar el polling de Telegram")

    threading.Thread(target=_start_telegram_polling_local, name="tg-polling", daemon=True).start()
    log.info(f"Sirviendo Flask en 0.0.0.0:{port}")
    serve(app_flask, host="0.0.0.0", port=port)
