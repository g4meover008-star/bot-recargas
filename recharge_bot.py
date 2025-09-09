import os
import io
import uuid
import logging
from datetime import datetime
from threading import Thread

from flask import Flask, request
from waitress import serve

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
)
from telegram.ext import (
    ApplicationBuilder, Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters
)

# Supabase (probado con supabase==1.0.3)
from supabase import create_client, Client

# ================= LOGGING =================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("recargas")

# ============== VARIABLES DE ENTORNO ==============
TG_BOT_TOKEN = os.getenv("TG_RECHARGE_BOT_TOKEN", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_API_KEY") or os.getenv("SUPABASE_ANON_KEY") or ""
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "")
PRICE_PER_CREDIT = float(os.getenv("PRICE_PER_CREDIT", "1"))
YAPE_QR_URL = os.getenv("YAPE_QR_URL", "")

BRAND_NAME = os.getenv("BRAND_NAME", "Tu tienda")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")  # opcional

if not (TG_BOT_TOKEN and SUPABASE_URL and SUPABASE_KEY and PUBLIC_BASE_URL):
    raise SystemExit("Faltan variables de entorno obligatorias.")

# =============== CLIENTES GLOBALES ===============
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
app_flask = Flask(__name__)
app: Application = ApplicationBuilder().token(TG_BOT_TOKEN).build()

# =============== HELPERS DB =================
def db_upsert_pago(
    pago_id: str, user_id: str, username: str,
    qty: int, amount: float, init_point: str
):
    """Crea/actualiza un pedido en la tabla 'pagos'."""
    supabase.table("pagos").upsert({
        "id": pago_id,
        "user_id": str(user_id),
        "username": username,
        "amount": float(amount),
        "quantity": int(qty),
        "status": "pendiente",
        "preference_id": "-",                  # no se usa con Yape, lo dejamos fijo
        "init_point": init_point,              # reutilizo este campo como URL QR
        "created_at": datetime.utcnow().isoformat()
    }, on_conflict="id").execute()

def db_get_pago(pago_id: str):
    r = supabase.table("pagos").select("*").eq("id", pago_id).limit(1).execute()
    return r.data[0] if r.data else None

def db_set_status(pago_id: str, new_status: str):
    supabase.table("pagos").update({
        "status": new_status,
        "updated_at": datetime.utcnow().isoformat()
    }).eq("id", pago_id).execute()

# =============== MENÚ / ESTADOS ===============
MENU = InlineKeyboardMarkup(
    [[InlineKeyboardButton("Créditos", callback_data="menu:credits")]]
)

# =============== HANDLERS ===============
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Bienvenida con botones."""
    text = (
        f"👋 <b>Bienvenido.</b>\n"
        f"Marca actual: <b>{BRAND_NAME}</b>\n"
        f"Tarifa: <b>{PRICE_PER_CREDIT:.2f} PEN</b> por crédito.\n\n"
        "Selecciona una opción:"
    )
    if update.message:
        await update.message.reply_text(text, reply_markup=MENU, parse_mode="HTML")
    else:
        await update.callback_query.message.reply_text(
            text, reply_markup=MENU, parse_mode="HTML"
        )

async def on_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Botones del menú."""
    q = update.callback_query
    data = q.data
    await q.answer()

    if data == "menu:credits":
        context.user_data["await_qty"] = True
        await q.message.reply_text(
            "Tarifa actual: "
            f"<b>{PRICE_PER_CREDIT:.2f} PEN</b> por crédito.\n\n"
            "¿Cuántas <b>cuentas</b> deseas comprar? (responde con un número)",
            parse_mode="HTML"
        )

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Captura la cantidad de cuentas cuando está activo el flujo."""
    if not context.user_data.get("await_qty"):
        return

    msg = (update.message.text or "").strip()
    try:
        qty = int(msg)
        if qty <= 0:
            raise ValueError()
    except Exception:
        await update.message.reply_text("❌ Envía solo un número válido. Ej: 10")
        return

    # cálculo
    amount = qty * PRICE_PER_CREDIT
    pago_id = str(uuid.uuid4())

    # guardamos pedido con el 'init_point' = URL con endpoint de QR (nuestro)
    qr_endpoint = f"{PUBLIC_BASE_URL}/qr/{pago_id}"
    db_upsert_pago(
        pago_id=pago_id,
        user_id=str(update.effective_user.id),
        username=update.effective_user.username or "",
        qty=qty,
        amount=amount,
        init_point=qr_endpoint
    )

    # guardo datos en user_data por si se necesitan en callbacks
    context.user_data.pop("await_qty", None)
    context.user_data["last_order"] = {"id": pago_id, "qty": qty, "amount": amount}

    caption = (
        f"<b>Pedido {pago_id[:8]}</b>\n"
        f"Importe: <b>{amount:.2f} PEN</b>\n"
        f"Créditos que recibirás: <b>{qty}</b>\n\n"
        "1) Pulsa <b>Ver QR</b> para abrir o escanear el código.\n"
        "2) Paga el <b>monto exacto</b>.\n"
        "3) Envía la <b>captura</b> del pago en este chat."
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🧾 Ver QR", callback_data=f"qr:{pago_id}")],
        [InlineKeyboardButton("Cancelar solicitud", callback_data=f"cancel:{pago_id}")]
    ])
    await update.message.reply_text(caption, reply_markup=kb, parse_mode="HTML")

async def on_qr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Envía el QR a partir del callback."""
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    pago_id = data.split(":", 1)[1]

    row = db_get_pago(pago_id)
    if not row:
        await q.message.reply_text("No encuentro ese pedido.")
        return

    # Preferimos enviar imagen directa del QR si YAPE_QR_URL es una imagen
    if YAPE_QR_URL and any(YAPE_QR_URL.lower().endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".gif")):
        try:
            await q.message.reply_photo(YAPE_QR_URL, caption="Escanea o abre el QR para pagar.")
        except Exception:
            await q.message.reply_text("Abre el QR para pagar:", reply_markup=None)
            await q.message.reply_text(YAPE_QR_URL)
    elif YAPE_QR_URL:
        await q.message.reply_text("Abre el QR para pagar:", reply_markup=None)
        await q.message.reply_text(YAPE_QR_URL)
    else:
        await q.message.reply_text("⚠️ No hay QR configurado. Contacta con soporte.")

async def on_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancela el pedido."""
    q = update.callback_query
    await q.answer()
    pago_id = q.data.split(":", 1)[1]
    db_set_status(pago_id, "cancelado")
    context.user_data.pop("last_order", None)
    await q.message.reply_text("🔸 Solicitud cancelada.")
    await cmd_start(update, context)

def _route_name(p: str) -> str:
    return p.strip("/").split("/", 1)[0]

# =============== FLASK ===============
@app_flask.route("/health", methods=["GET"])
def health():
    return "ok", 200

@app_flask.route("/qr/<pago_id>", methods=["GET"])
def qr_redirect(pago_id: str):
    """Endpoint 'falso' por compatibilidad (devolvemos el QR fijo)."""
    # Si quisieras devolver PNG generado, podrías hacerlo aquí.
    html = f"""
    <html><body>
    <h3>Pedido {pago_id}</h3>
    <p>Escanea el QR o ábrelo en otra pestaña:</p>
    <p><a href="{YAPE_QR_URL}" target="_blank">{YAPE_QR_URL}</a></p>
    </body></html>
    """
    return html, 200

# =============== REGISTRO DE HANDLERS ===============
app.add_handler(CommandHandler("start", cmd_start))
app.add_handler(CallbackQueryHandler(on_menu, pattern=r"^menu:"))
app.add_handler(CallbackQueryHandler(on_qr, pattern=r"^qr:"))
app.add_handler(CallbackQueryHandler(on_cancel, pattern=r"^cancel:"))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

# =============== MAIN (Bot en hilo, Flask en principal) ===============
def run_bot():
    log.info("Recargas %s iniciando…", BRAND_NAME)
    if YAPE_QR_URL:
        log.info("YAPE_QR_URL: definido")
    else:
        log.warning("YAPE_QR_URL NO definido")

    # Bloqueante, pero lo ejecutamos en un Thread
    app.run_polling(allowed_updates=Update.ALL_TYPES, close_loop=True)

if __name__ == "__main__":
    # Arrancamos el bot en un hilo para evitar conflictos con el loop
    t = Thread(target=run_bot, daemon=True)
    t.start()

    port = int(os.getenv("PORT", "8080"))
    log.info("HTTP escuchando en 0.0.0.0:%s", port)
    serve(app_flask, host="0.0.0.0", port=port)
