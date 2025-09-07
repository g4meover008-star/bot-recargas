import os
import logging
from datetime import datetime
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
from supabase import create_client, Client

# ---------------- LOGGING ----------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("recargas")

# ---------------- ENV ----------------
TG_BOT_TOKEN = os.getenv("TG_RECHARGE_BOT_TOKEN", "")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "2016769834"))  # tu ID
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_API_KEY", "")
PRICE_PER_CREDIT = float(os.getenv("PRICE_PER_CREDIT", "25.0"))  # 25 soles

if not (TG_BOT_TOKEN and SUPABASE_URL and SUPABASE_KEY):
    raise SystemExit("Faltan variables de entorno (TG_RECHARGE_BOT_TOKEN, SUPABASE_URL, SUPABASE_API_KEY)")

# ---------------- SUPABASE ----------------
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

def user_add_credits(user_id: str, cantidad: int):
    """Agrega créditos al usuario en Supabase"""
    r = supabase.table("usuarios").select("creditos").eq("telegram_id", str(user_id)).limit(1).execute()
    current = int(r.data[0]["creditos"]) if r.data and r.data[0].get("creditos") else 0
    new_value = current + cantidad

    # Actualizar créditos
    supabase.table("usuarios").upsert({
        "telegram_id": str(user_id),
        "creditos": new_value
    }, on_conflict="telegram_id").execute()

    # Guardar historial
    try:
        supabase.table("creditos_historial").insert({
            "usuario_id": str(user_id),
            "delta": cantidad,
            "motivo": "recarga_manual",
            "hecho_por": "admin",
            "created_at": datetime.utcnow().isoformat()
        }).execute()
    except Exception as e:
        log.warning(f"No se pudo guardar historial: {e}")

    return new_value


# ---------------- BOT ----------------
tg_app = ApplicationBuilder().token(TG_BOT_TOKEN).build()

# Estados temporales en memoria
pending_orders = {}  # user_id -> {"cantidad": x, "total": y, "time": t}


# ---------------- HANDLERS ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "👋 Bienvenido a Bot de Recargas.\n\n"
        f"Tarifa: {PRICE_PER_CREDIT:.2f} PEN por crédito (1 cuenta).\n\n"
        "Selecciona una opción:"
    )
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("💳 Recargar créditos", callback_data="recargar")]]
    )
    await update.message.reply_text(text, reply_markup=kb)


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "recargar":
        await query.message.reply_text(
            f"Tarifa: {PRICE_PER_CREDIT:.2f} PEN por cuenta.\n"
            "¿Cuántas cuentas deseas comprar?"
        )
        context.user_data["waiting_amount"] = True


async def on_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("waiting_amount"):
        return

    try:
        cantidad = int(update.message.text.strip())
        if cantidad <= 0:
            raise ValueError()
    except Exception:
        await update.message.reply_text("❌ Ingresa un número válido de cuentas.")
        return

    total = cantidad * PRICE_PER_CREDIT
    user = update.effective_user

    # Guardar orden en memoria
    pending_orders[user.id] = {
        "cantidad": cantidad,
        "total": total,
        "time": datetime.utcnow().isoformat(),
    }

    # Mostrar al cliente
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("📷 Ver QR de pago (Yape)", callback_data="show_qr")]]
    )
    await update.message.reply_text(
        f"🧾 Pedido: {cantidad} cuenta(s).\n"
        f"💵 Total a pagar: {total:.2f} PEN\n\n"
        "Escanea el QR y avísanos cuando pagues.",
        reply_markup=kb,
    )

    # Notificar al admin
    kb_admin = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Aprobar", callback_data=f"aprobar:{user.id}"),
            InlineKeyboardButton("❌ Rechazar", callback_data=f"rechazar:{user.id}")
        ]
    ])
    await context.bot.send_message(
        chat_id=ADMIN_CHAT_ID,
        text=(
            f"📥 Nueva solicitud de {user.username or user.id}\n"
            f"Cantidad: {cantidad}\n"
            f"Total: {total:.2f} PEN\n"
            f"User ID: {user.id}"
        ),
        reply_markup=kb_admin,
    )

    context.user_data["waiting_amount"] = False


async def show_qr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    order = pending_orders.get(user_id)
    if not order:
        await query.message.reply_text("⚠️ No encontré tu pedido.")
        return

    # Aquí pones tu QR de Yape (imagen o link)
    await query.message.reply_text(
        "📲 Escanea este QR con Yape y paga el monto indicado.\n\n"
        "Luego espera la confirmación del administrador."
    )
    try:
        with open("qr_yape.png", "rb") as f:  # asegúrate de subir qr_yape.png a tu servidor
            await query.message.reply_photo(f, caption="QR Yape")
    except Exception:
        await query.message.reply_text("⚠️ No se pudo cargar el QR, contacta con soporte.")


async def on_admin_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data.split(":")
    action, user_id = data[0], int(data[1])
    order = pending_orders.get(user_id)

    if not order:
        await query.message.reply_text("⚠️ Pedido ya no existe.")
        return

    if action == "aprobar":
        cantidad = order["cantidad"]
        new_total = user_add_credits(user_id, cantidad)

        await context.bot.send_message(
            chat_id=user_id,
            text=(
                f"✅ Pago confirmado.\n"
                f"Se acreditaron {cantidad} crédito(s).\n"
                f"Tu saldo actual es {new_total} créditos."
            ),
        )
        await query.message.edit_text(f"✔ Pedido de {user_id} aprobado.")
    elif action == "rechazar":
        await context.bot.send_message(
            chat_id=user_id,
            text="❌ Tu pago fue rechazado. Contacta con soporte."
        )
        await query.message.edit_text(f"✖ Pedido de {user_id} rechazado.")

    # Borrar de pedidos pendientes
    pending_orders.pop(user_id, None)


# ---------------- MAIN ----------------
def main():
    tg_app.add_handler(CommandHandler("start", start))
    tg_app.add_handler(CallbackQueryHandler(on_button, pattern="^recargar$"))
    tg_app.add_handler(CallbackQueryHandler(show_qr, pattern="^show_qr$"))
    tg_app.add_handler(CallbackQueryHandler(on_admin_action, pattern="^(aprobar|rechazar):"))
    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_amount))

    tg_app.run_polling()


if __name__ == "__main__":
    main()
