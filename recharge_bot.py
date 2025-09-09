import os
import uuid
import logging
from datetime import datetime
from threading import Thread

import requests
from flask import Flask, request
from waitress import serve

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.ext import (
    ApplicationBuilder, Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters
)

# ================= LOGGING =================
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("recargas")

# ============== ENV ==============
TG_BOT_TOKEN = os.getenv("TG_RECHARGE_BOT_TOKEN", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_API_KEY") or os.getenv("SUPABASE_ANON_KEY") or ""
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "")
PRICE_PER_CREDIT = float(os.getenv("PRICE_PER_CREDIT", "1"))
YAPE_QR_URL = os.getenv("YAPE_QR_URL", "")

BRAND_NAME = os.getenv("BRAND_NAME", "Tu tienda")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")  # opcional

if not (TG_BOT_TOKEN and SUPABASE_URL and SUPABASE_KEY and PUBLIC_BASE_URL):
    raise SystemExit("Faltan variables de entorno: TG_RECHARGE_BOT_TOKEN, SUPABASE_URL, SUPABASE_API_KEY/ANON, PUBLIC_BASE_URL")

# ============== Supabase REST helpers ==============
def sb_headers(prefer_return="representation", extra=None):
    h = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": f"return={prefer_return}",
    }
    if extra:
        h.update(extra)
    return h

def db_upsert_pago(pago_id: str, user_id: str, username: str, qty: int, amount: float, qr_endpoint: str):
    """Crea/actualiza pedido en 'pagos'. Requiere que 'id' sea PRIMARY KEY o UNIQUE para que el upsert funcione."""
    url = f"{SUPABASE_URL}/rest/v1/pagos"
    payload = [{
        "id": pago_id,
        "user_id": str(user_id),
        "username": username,
        "amount": float(amount),
        "quantity": int(qty),
        "status": "pendiente",
        "preference_id": "-",       # compat
        "init_point": qr_endpoint,  # guardamos aquí URL del QR/endpoint
        "created_at": datetime.utcnow().isoformat()
    }]
    # Upsert por PK: Prefer: resolution=merge-duplicates
    r = requests.post(url, json=payload, headers=sb_headers(extra={"Prefer": "resolution=merge-duplicates"}))
    r.raise_for_status()
    return r.json()[0] if r.text else None

def db_get_pago(pago_id: str):
    url = f"{SUPABASE_URL}/rest/v1/pagos"
    params = {
        "id": f"eq.{pago_id}",
        "select": "*",
        "limit": 1
    }
    r = requests.get(url, params=params, headers=sb_headers(prefer_return="minimal"))
    r.raise_for_status()
    data = r.json()
    return data[0] if data else None

def db_set_status(pago_id: str, new_status: str):
    url = f"{SUPABASE_URL}/rest/v1/pagos"
    params = {"id": f"eq.{pago_id}"}
    payload = {"status": new_status, "updated_at": datetime.utcnow().isoformat()}
    r = requests.patch(url, params=params, json=payload, headers=sb_headers())
    r.raise_for_status()
    return r.json()[0] if r.text else None

# ============== Telegram UI ==============
MENU = InlineKeyboardMarkup([[InlineKeyboardButton("Créditos", callback_data="menu:credits")]])

# ============== Flask ==============
app_flask = Flask(__name__)

@app_flask.route("/health", methods=["GET"])
def health():
    return "ok", 200

@app_flask.route("/qr/<pago_id>", methods=["GET"])
def qr_redirect(pago_id: str):
    html = f"""
    <html><body>
      <h3>Pedido {pago_id}</h3>
      <p>Abre el QR en otra pestaña:</p>
      <p><a href="{YAPE_QR_URL}" target="_blank">{YAPE_QR_URL}</a></p>
    </body></html>
    """
    return html, 200

# ============== Telegram Handlers ==============
app: Application = ApplicationBuilder().token(TG_BOT_TOKEN).build()

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        f"👋 <b>Bienvenido.</b>\n"
        f"Marca: <b>{BRAND_NAME}</b>\n"
        f"Tarifa: <b>{PRICE_PER_CREDIT:.2f} PEN</b> por crédito.\n\n"
        "Selecciona una opción:"
    )
    if update.message:
        await update.message.reply_text(text, reply_markup=MENU, parse_mode="HTML")
    else:
        await update.callback_query.message.reply_text(text, reply_markup=MENU, parse_mode="HTML")

async def on_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "menu:credits":
        context.user_data["await_qty"] = True
        await q.message.reply_text(
            "Tarifa actual: "
            f"<b>{PRICE_PER_CREDIT:.2f} PEN</b> por crédito.\n\n"
            "¿Cuántas <b>cuentas</b> deseas comprar? (responde con un número)",
            parse_mode="HTML"
        )

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("await_qty"):
        return

    txt = (update.message.text or "").strip()
    try:
        qty = int(txt)
        if qty <= 0:
            raise ValueError()
    except Exception:
        await update.message.reply_text("❌ Envía un número válido. Ej: 10")
        return

    amount = qty * PRICE_PER_CREDIT
    pago_id = str(uuid.uuid4())
    qr_endpoint = f"{PUBLIC_BASE_URL}/qr/{pago_id}"

    try:
        db_upsert_pago(
            pago_id=pago_id,
            user_id=str(update.effective_user.id),
            username=update.effective_user.username or "",
            qty=qty,
            amount=amount,
            qr_endpoint=qr_endpoint
        )
    except Exception as e:
        log.exception("Error guardando pedido")
        await update.message.reply_text("⚠️ No pude crear el pedido. Intenta de nuevo en unos minutos.")
        return

    context.user_data.pop("await_qty", None)
    context.user_data["last_order"] = {"id": pago_id, "qty": qty, "amount": amount}

    caption = (
        f"<b>Pedido {pago_id[:8]}</b>\n"
        f"Importe: <b>{amount:.2f} PEN</b>\n"
        f"Créditos que recibirás: <b>{qty}</b>\n\n"
        "1) Pulsa <b>Ver QR</b> para abrir/escANear.\n"
        "2) Paga el <b>monto exacto</b>.\n"
        "3) Envía la <b>captura</b> del pago en este chat."
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🧾 Ver QR", callback_data=f"qr:{pago_id}")],
        [InlineKeyboardButton("Cancelar solicitud", callback_data=f"cancel:{pago_id}")]
    ])
    await update.message.reply_text(caption, reply_markup=kb, parse_mode="HTML")

async def on_qr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    pago_id = (q.data or "").split(":", 1)[1]

    row = db_get_pago(pago_id)
    if not row:
        await q.message.reply_text("No encuentro ese pedido.")
        return

    if YAPE_QR_URL and any(YAPE_QR_URL.lower().endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".gif")):
        try:
            await q.message.reply_photo(YAPE_QR_URL, caption="Escanea o abre el QR para pagar.")
        except Exception:
            await q.message.reply_text("Abre el QR para pagar:")
            await q.message.reply_text(YAPE_QR_URL)
    elif YAPE_QR_URL:
        await q.message.reply_text("Abre el QR para pagar:")
        await q.message.reply_text(YAPE_QR_URL)
    else:
        await q.message.reply_text("⚠️ No hay QR configurado.")

async def on_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    pago_id = q.data.split(":", 1)[1]
    try:
        db_set_status(pago_id, "cancelado")
    except Exception:
        pass
    context.user_data.pop("last_order", None)
    await q.message.reply_text("🔸 Solicitud cancelada.")
    await cmd_start(update, context)

# Registro
app.add_handler(CommandHandler("start", cmd_start))
app.add_handler(CallbackQueryHandler(on_menu, pattern=r"^menu:"))
app.add_handler(CallbackQueryHandler(on_qr, pattern=r"^qr:"))
app.add_handler(CallbackQueryHandler(on_cancel, pattern=r"^cancel:"))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

# ========= MAIN =========
if __name__ == "__main__":
    # Railway expone el puerto en $PORT
    port = int(os.getenv("PORT", "8080"))

    def run_bot():
        """
        Arranca el bot de Telegram en un hilo independiente
        con su propio event loop de asyncio.
        """
        # Crear y asignar event loop para este hilo
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        # Arrancar el polling (bloqueante) usando ese loop
        # IMPORTANTE: no usar close_loop=True aquí
        from telegram import Update  # por si no está en el scope del hilo
        log.info("Iniciando bot de Telegram (polling)…")
        tg_app.run_polling(allowed_updates=Update.ALL_TYPES)

    # Bot en un hilo “daemon” para que el proceso principal (waitress) pueda
    # terminar si algo va mal sin quedar colgado
    t = threading.Thread(target=run_bot, name="run_bot", daemon=True)
    t.start()

    # Arrancar el servidor HTTP (Flask) en el hilo principal
    from waitress import serve
    log.info(f"HTTP escuchando en 0.0.0.0:{port}")
    serve(app_flask, host="0.0.0.0", port=port)
