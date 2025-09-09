import os
import asyncio
import logging
from flask import Flask

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
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

# =============== ENV VARS ==================
TG_BOT_TOKEN    = os.getenv("TG_RECHARGE_BOT_TOKEN", "").strip()
ADMIN_CHAT_ID   = os.getenv("ADMIN_CHAT_ID", "").strip()   # opcional
BRAND_NAME      = os.getenv("BRAND_NAME", "Recargas").strip()
PRICE_PER_CREDIT = float(os.getenv("PRICE_PER_CREDIT", "1.0"))
YAPE_QR_URL     = os.getenv("YAPE_QR_URL", "").strip()     # http(s) o file_id de Telegram
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip() # opcional, solo para /health

if not TG_BOT_TOKEN:
    raise SystemExit("Falta la variable TG_RECHARGE_BOT_TOKEN")

# ============== FLASK (health) =============
app_flask = Flask(__name__)

@app_flask.route("/health", methods=["GET"])
def health():
    return "ok", 200

# ============= TELEGRAM APP ================
app = ApplicationBuilder().token(TG_BOT_TOKEN).build()

# ---------- helpers ----------
def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("💰 Recargar", callback_data="menu:recargar")],
            # agrega más botones si quieres
            [InlineKeyboardButton("ℹ️ Ayuda", callback_data="menu:ayuda")],
        ]
    )

async def ask_amount(update_or_query, context: ContextTypes.DEFAULT_TYPE):
    """Pide el monto a recargar y setea el estado."""
    context.user_data["await_amount"] = True
    text = (
        "¿Cuánto quieres recargar? Escribe sólo el número en soles.\n"
        "Ejemplos: 5  |  10.50  |  25\n"
        "(Monto en S/)"
    )
    if isinstance(update_or_query, Update) and update_or_query.message:
        await update_or_query.message.reply_text(text)
    else:
        q = update_or_query.callback_query
        await q.message.reply_text(text)

async def show_qr_for_amount(query, amount: float):
    """Envía el QR de Yape con el monto que debe pagar."""
    caption = (
        f"📷 *QR de Yape*\n"
        f"Monto a pagar: S/ {amount:.2f}\n\n"
        f"👉 Paga el monto exacto y *envía la captura* por aquí para validar."
    )
    # Evitamos parse_mode para no tener problemas con entidades
    try:
        if YAPE_QR_URL.startswith("http"):
            await query.message.reply_photo(YAPE_QR_URL, caption=caption)
        else:
            # Si guardaste un file_id de Telegram en YAPE_QR_URL
            await query.message.reply_photo(YAPE_QR_URL, caption=caption)
    except Exception as e:
        log.exception("Error enviando QR")
        await query.message.reply_text(
            "No pude enviar la imagen del QR. Verifica YAPE_QR_URL en las variables."
        )

# ---------- handlers ----------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (
        f"👋 Bienvenido a *{BRAND_NAME}*.\n\n"
        "Selecciona una opción:"
    )
    # sin parse_mode para evitar errores con etiquetas
    await update.message.reply_text(txt, reply_markup=main_menu_keyboard())

async def on_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""

    if data == "menu:recargar":
        await ask_amount(update, context)

    elif data == "menu:ayuda":
        await q.message.reply_text(
            "💡 Ayuda\n\n"
            "1) Elige *Recargar*.\n"
            "2) Escribe el *monto en soles* que quieres recargar.\n"
            "3) Pulsa *Ver QR*, paga y envía la captura.\n"
            "4) Validamos y acreditamos tu recarga.\n"
        )

    elif data.startswith("qr|"):
        try:
            amount = float(data.split("|", 1)[1])
        except Exception:
            amount = 0.0
        await show_qr_for_amount(q, amount)

    elif data == "cancel":
        context.user_data.pop("await_amount", None)
        await q.message.reply_text("Operación cancelada.", reply_markup=main_menu_keyboard())

async def on_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cuando el usuario está en estado 'await_amount', interpretamos su texto como monto."""
    if not context.user_data.get("await_amount"):
        # Mensaje fuera de flujo -> re‑muestra el menú
        await update.message.reply_text("Elige una opción:", reply_markup=main_menu_keyboard())
        return

    raw = (update.message.text or "").replace(",", ".").strip()
    try:
        amount = float(raw)
        if amount <= 0:
            raise ValueError
    except Exception:
        await update.message.reply_text("Monto inválido. Ejemplos válidos: 5  |  10.50  |  25")
        return

    # Guardamos y salimos del estado
    context.user_data["await_amount"] = False
    context.user_data["amount"] = amount

    # Ofrecemos botón para ver QR y cancelar
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🧾 Ver QR", callback_data=f"qr|{amount:.2f}")],
            [InlineKeyboardButton("❌ Cancelar", callback_data="cancel")],
        ]
    )
    txt = (
        f"Has solicitado recargar: S/ {amount:.2f}\n\n"
        "Pulsa *Ver QR* para ver la imagen y realizar el pago.\n"
        "Luego envía la *captura* de tu pago para validarlo."
    )
    await update.message.reply_text(txt, reply_markup=kb)

async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Menú principal:", reply_markup=main_menu_keyboard())

# ============== registro de handlers ==============
app.add_handler(CommandHandler("start", cmd_start))
app.add_handler(CommandHandler("menu", cmd_menu))
app.add_handler(CallbackQueryHandler(on_menu_callback, pattern=r"^(menu:|qr\|.|cancel)"))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text_message))

# =================== MAIN ========================
if __name__ == "__main__":
    log.info("Recargas %s iniciando…", BRAND_NAME)
    log.info("YAPE_QR_URL: %s", "definido" if YAPE_QR_URL else "NO definido")

    port = int(os.getenv("PORT", "8080"))

    async def run_all():
        # Lanza el bot en segundo plano (sin cerrar el loop)
        asyncio.create_task(app.run_polling(close_loop=False))
        # Sirve Flask con waitress
        from waitress import serve
        serve(app_flask, host="0.0.0.0", port=port)

    asyncio.run(run_all())
