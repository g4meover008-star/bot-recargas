import os, asyncio, logging, threading, uuid
from datetime import datetime, timezone
from typing import Optional, Dict, Any

import requests
from flask import Flask

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ================= LOGGING =================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("recargas")


# ================= ENV =================
TG_BOT_TOKEN = os.getenv("TG_RECHARGE_BOT_TOKEN", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_API_KEY") or os.getenv("SUPABASE_ANON_KEY") or ""
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "")
PRICE_PER_CREDIT = float(os.getenv("PRICE_PER_CREDIT", "1"))
BRAND_NAME = os.getenv("BRAND_NAME", "Recargas")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0"))
YAPE_QR_URL = os.getenv("YAPE_QR_URL", "")

MIN_ACCOUNTS = int(os.getenv("MIN_ACCOUNTS", "1"))
CREDITS_PER_ACCOUNT = int(os.getenv("CREDITS_PER_ACCOUNT", "1"))

if not (TG_BOT_TOKEN and SUPABASE_URL and SUPABASE_KEY and PUBLIC_BASE_URL and ADMIN_CHAT_ID and YAPE_QR_URL):
    raise SystemExit(
        "Faltan variables: TG_RECHARGE_BOT_TOKEN, SUPABASE_URL, SUPABASE_API_KEY/ANON, "
        "PUBLIC_BASE_URL, ADMIN_CHAT_ID, YAPE_QR_URL, PRICE_PER_CREDIT"
    )

# ================= APPs =================
app_flask = Flask(__name__)

# Telegram Application
app = ApplicationBuilder().token(TG_BOT_TOKEN).build()

# ================= SUPABASE REST HELPERS =================
SB_BASE = SUPABASE_URL.rstrip("/") + "/rest/v1"
SB_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=representation",
}

def sb_post(table: str, payload: Dict[str, Any]) -> list[dict]:
    r = requests.post(f"{SB_BASE}/{table}", headers=SB_HEADERS, json=payload)
    r.raise_for_status()
    return r.json() if r.text else []

def sb_patch(table: str, payload: Dict[str, Any], filters: Dict[str, Any]) -> list[dict]:
    params = {}
    for k, v in filters.items():
        params[k] = f"eq.{v}"
    r = requests.patch(f"{SB_BASE}/{table}", headers=SB_HEADERS, params=params, json=payload)
    r.raise_for_status()
    return r.json() if r.text else []

def sb_get_one(table: str, filters: Dict[str, Any], order_by: Optional[str]=None, desc: bool=True) -> Optional[dict]:
    params = {"select": "*", "limit": 1}
    for k, v in filters.items():
        params[k] = f"eq.{v}"
    if order_by:
        params["order"] = f"{order_by}.{'desc' if desc else 'asc'}"
    r = requests.get(f"{SB_BASE}/{table}", headers=SB_HEADERS, params=params)
    r.raise_for_status()
    data = r.json()
    return data[0] if data else None

def sb_get_many(table: str, filters: Dict[str, Any], limit: int = 100, order_by: Optional[str]=None, desc: bool=True) -> list[dict]:
    params = {"select": "*", "limit": limit}
    for k, v in filters.items():
        params[k] = f"eq.{v}"
    if order_by:
        params["order"] = f"{order_by}.{'desc' if desc else 'asc'}"
    r = requests.get(f"{SB_BASE}/{table}", headers=SB_HEADERS, params=params)
    r.raise_for_status()
    return r.json()

def add_credits(telegram_id: int, username: str, add: int) -> int:
    """Suma créditos en tabla usuarios (crea si no existe). Retorna total final."""
    user = sb_get_one("usuarios", {"telegram_id": str(telegram_id)})
    if not user:
        new_user = {
            "telegram_id": str(telegram_id),
            "username": username or "",
            "creditos": add,
        }
        sb_post("usuarios", new_user)
        return add
    else:
        current = int(user.get("creditos") or 0)
        new_total = current + add
        sb_patch("usuarios", {"creditos": new_total}, {"telegram_id": str(telegram_id)})
        return new_total


# ================= UTIL =================
def soles(n: float) -> str:
    return f"{n:.2f} PEN"

def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# =================== TELEGRAM HANDLERS ===================

WELCOME_TXT = (
    "👋 <b>Bienvenido.</b>\n"
    f"Marca: <b>{BRAND_NAME}</b>\n"
    f"Tarifa: <b>{PRICE_PER_CREDIT:.2f}</b> por crédito.\n\n"
    "Selecciona una opción:"
)

def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 Recargar", callback_data="recargar")],
        [InlineKeyboardButton("ℹ️ Ayuda", callback_data="ayuda")],
    ])

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    await context.bot.send_message(
        chat_id=chat.id,
        text=WELCOME_TXT,
        parse_mode="HTML",
        reply_markup=main_menu_kb()
    )

async def on_menu_press(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    if data == "ayuda":
        await q.message.reply_text(
            "• Usa <code>/recargar</code> o el botón <b>Recargar</b>.\n"
            f"• Ingresa la <b>cantidad de cuentas</b> (mínimo {MIN_ACCOUNTS}).\n"
            "• Recibirás el importe y el botón <b>Ver QR</b> para pagar.\n"
            "• Envía la <b>captura</b> del pago en el chat.\n"
            "• El admin confirmará y tus créditos se acreditarán.",
            parse_mode="HTML"
        )
    elif data == "recargar":
        await ask_qty(update, context, via_button=True)

# ---- Flujo: /recargar -> pedir cantidad ----
async def cmd_recargar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ask_qty(update, context, via_button=False)

async def ask_qty(update_or_query, context: ContextTypes.DEFAULT_TYPE, via_button: bool):
    if isinstance(update_or_query, Update) and update_or_query.callback_query:
        q = update_or_query.callback_query
        msg_target = q.message
    else:
        msg_target = update_or_query.message

    await msg_target.reply_text(
        f"Tarifa: <b>{PRICE_PER_CREDIT:.2f}</b> por crédito.\n"
        f"¿Cuántas cuentas deseas comprar? (mínimo {MIN_ACCOUNTS})",
        parse_mode="HTML"
    )
    # Guardamos bandera de espera
    context.user_data["waiting_qty"] = True

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cuando el usuario escribe la cantidad de cuentas."""
    if not context.user_data.get("waiting_qty"):
        return  # ignorar textos que no son para cantidad

    try:
        qty = int(update.message.text.strip())
    except Exception:
        await update.message.reply_text("Ingresa un número válido. Ej: 10")
        return

    if qty < MIN_ACCOUNTS:
        await update.message.reply_text(f"El mínimo es {MIN_ACCOUNTS}.")
        return

    # Creamos pedido
    total = qty * PRICE_PER_CREDIT
    credits_to_add = qty * CREDITS_PER_ACCOUNT
    order_id = str(uuid.uuid4())

    user = update.effective_user
    sb_post("pagos", {
        "id": order_id,
        "user_id": str(user.id),
        "username": user.username or "",
        "qty": qty,
        "amount": float(total),
        "credits": credits_to_add,
        "status": "pendiente",
        "created_at": utcnow_iso()
    })

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🧾 Ver QR", callback_data=f"qr:{order_id}")],
        [InlineKeyboardButton("❌ Cancelar solicitud", callback_data=f"cancel:{order_id}")]
    ])

    txt = (
        f"<b>Pedido</b> <code>{order_id[:8]}</code>\n"
        f"Importe: <b>{soles(total)}</b>\n"
        f"Créditos que recibirás: <b>{credits_to_add}</b>\n\n"
        "1) Pulsa <b>Ver QR</b> para pagar.\n"
        "2) Paga el monto exacto.\n"
        "3) Envía <b>la captura</b> del pago aquí."
    )
    await update.message.reply_text(txt, parse_mode="HTML", reply_markup=kb)
    context.user_data["waiting_qty"] = False

async def on_qr_or_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    if data.startswith("qr:"):
        order_id = data.split(":", 1)[1]
        pedido = sb_get_one("pagos", {"id": order_id})
        if not pedido or pedido.get("status") != "pendiente":
            await q.message.reply_text("No encuentro el pedido pendiente.")
            return
        caption = (
            f"Pedido <code>{order_id[:8]}</code>\n"
            f"Importe: <b>{soles(pedido['amount'])}</b>\n"
            "Escanea o abre el QR para pagar."
        )
        await q.message.reply_photo(YAPE_QR_URL, caption=caption, parse_mode="HTML")

    elif data.startswith("cancel:"):
        order_id = data.split(":", 1)[1]
        sb_patch("pagos", {"status": "cancelado", "updated_at": utcnow_iso()}, {"id": order_id})
        await q.message.reply_text("Solicitud cancelada.")


# ---- Captura del pago ----
async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    # Tomamos el último pedido pendiente
    pedido = sb_get_one("pagos", {"user_id": str(user.id), "status": "pendiente"}, order_by="created_at", desc=True)
    if not pedido:
        await update.message.reply_text("No encuentro un pedido pendiente. Usa /recargar.")
        return

    photo = update.message.photo[-1]
    file_id = photo.file_id

    # Guardamos evidencia
    sb_patch("pagos", {"proof_file_id": file_id, "updated_at": utcnow_iso()}, {"id": pedido["id"]})

    # Avisamos al admin con botones para aprobar/rechazar
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Aprobar", callback_data=f"admin_ok:{pedido['id']}"),
            InlineKeyboardButton("🚫 Rechazar", callback_data=f"admin_no:{pedido['id']}")
        ]
    ])
    caption = (
        f"🧾 Pedido <code>{pedido['id'][:8]}</code>\n"
        f"Usuario: <code>{user.id}</code> @{user.username or '-'}\n"
        f"Cantidad: <b>{pedido.get('qty')}</b> cuentas\n"
        f"Importe: <b>{soles(pedido['amount'])}</b>\n"
        f"Créditos: <b>{pedido.get('credits')}</b>\n"
        "¿Confirmar pago?"
    )
    await context.bot.send_photo(
        chat_id=ADMIN_CHAT_ID,
        photo=file_id,
        caption=caption,
        parse_mode="HTML",
        reply_markup=kb
    )
    await update.message.reply_text("📩 Recibí tu captura. En breve un administrador la revisará.")

# ---- Admin callbacks ----
async def on_admin_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    if not (data.startswith("admin_ok:") or data.startswith("admin_no:")):
        return

    order_id = data.split(":", 1)[1]
    pedido = sb_get_one("pagos", {"id": order_id})
    if not pedido:
        await q.message.reply_text("Pedido no encontrado.")
        return
    if pedido.get("status") == "aprobado":
        await q.message.reply_text("Este pedido ya fue aprobado.")
        return
    if pedido.get("status") == "rechazado":
        await q.message.reply_text("Este pedido ya fue rechazado.")
        return

    user_id = int(pedido["user_id"])
    credits = int(pedido.get("credits") or 0)

    if data.startswith("admin_ok:"):
        # Aprobar: marcar y sumar créditos
        sb_patch("pagos", {"status": "aprobado", "updated_at": utcnow_iso()}, {"id": order_id})
        new_total = add_credits(user_id, "", credits)
        # Avisos
        await q.message.reply_text("✅ Aprobado y acreditado.")
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    "✅ <b>Pago verificado</b>\n"
                    f"Créditos añadidos: <b>{credits}</b>\n"
                    f"Saldo actual: <b>{new_total}</b>"
                ),
                parse_mode="HTML"
            )
        except Exception:
            pass

    else:
        # Rechazar
        sb_patch("pagos", {"status": "rechazado", "updated_at": utcnow_iso()}, {"id": order_id})
        await q.message.reply_text("🚫 Rechazado.")
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text="🚫 Tu pago fue rechazado. Si crees que es un error, contáctanos."
            )
        except Exception:
            pass


# =================== FLASK ===================
@app_flask.route("/health", methods=["GET"])
def health():
    return "ok", 200


# =================== MAIN (HTTP + BOT) ===================
def _run_http():
    from waitress import serve
    port = int(os.getenv("PORT", "8080"))
    log.info(f"HTTP escuchando en 0.0.0.0:{port}")
    serve(app_flask, host="0.0.0.0", port=port)

def _run_bot():
    # Cada hilo necesita su propio event loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    log.info(f"Recargas {BRAND_NAME} iniciando…")
    log.info(f"YAPE_QR_URL: {'definido' if YAPE_QR_URL else 'NO definido'}")

    # Handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(on_menu_press, pattern=r"^(recargar|ayuda)$"))

    app.add_handler(CommandHandler("recargar", cmd_recargar))
    app.add_handler(CallbackQueryHandler(on_qr_or_cancel, pattern=r"^(qr:|cancel:)"))

    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), on_text))
    app.add_handler(CallbackQueryHandler(on_admin_action, pattern=r"^(admin_ok:|admin_no:)"))

    # Bot en polling
    app.run_polling(allowed_updates=Update.ALL_TYPES, close_loop=True)

if __name__ == "__main__":
    # 1) Bot en hilo
    t = threading.Thread(target=_run_bot, name="run_bot", daemon=True)
    t.start()
    # 2) HTTP en hilo principal (mantiene vivo el contenedor)
    _run_http()
