import os
import io
import uuid
import asyncio
import logging
import threading
from datetime import datetime

from supabase import create_client, Client
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

# -------------------- LOGGING --------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("recargas")

# -------------------- ENV --------------------
TG_BOT_TOKEN = os.getenv("TG_RECHARGE_BOT_TOKEN", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_ANON_KEY") or os.getenv("SUPABASE_API_KEY") or ""
ADMIN_ID = int(os.getenv("ADMIN_ID", "2016769834"))  # tu ID

PRECIO_POR_CUENTA = 25  # soles = 1 crédito

if not (TG_BOT_TOKEN and SUPABASE_URL and SUPABASE_KEY):
    raise SystemExit("Faltan variables de entorno")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
tg_app = ApplicationBuilder().token(TG_BOT_TOKEN).build()

# -------------------- DB helpers --------------------
def pedido_insert(user_id, username, cantidad, monto):
    pid = str(uuid.uuid4())
    supabase.table("pedidos").insert({
        "id": pid,
        "user_id": str(user_id),
        "username": username,
        "cantidad": cantidad,
        "monto": monto,
        "estado": "pendiente",
        "created_at": datetime.utcnow().isoformat()
    }).execute()
    return pid

def pedido_set_comprobante(pedido_id, url):
    supabase.table("pedidos").update({"comprobante_url": url}).eq("id", pedido_id).execute()

def pedido_set_estado(pedido_id, estado):
    supabase.table("pedidos").update({"estado": estado}).eq("id", pedido_id).execute()

def user_add_credits(user_id: str, cantidad: int):
    r = supabase.table("usuarios").select("creditos").eq("telegram_id", str(user_id)).limit(1).execute()
    current = int(r.data[0]["creditos"]) if r.data and r.data[0].get("creditos") else 0
    new_value = current + cantidad
    supabase.table("usuarios").update({"creditos": new_value}).eq("telegram_id", str(user_id)).execute()
    return new_value

# -------------------- Estados temporales --------------------
user_pending_orders = {}  # user_id -> pedido_id

# -------------------- HANDLERS --------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🟢 Recargar créditos", callback_data="recargar")]
    ])
    await update.message.reply_text(
        "👋 Bienvenido.\n"
        f"Tarifa: {PRECIO_POR_CUENTA} soles por cuenta.\n\n"
        "Selecciona una opción:",
        reply_markup=kb
    )

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if q.data == "recargar":
        await q.message.reply_text("¿Cuántas cuentas deseas comprar? (ej: 2)")
        context.user_data["awaiting_cantidad"] = True

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text

    if context.user_data.get("awaiting_cantidad"):
        try:
            cantidad = int(text)
            if cantidad <= 0:
                raise ValueError()
        except:
            await update.message.reply_text("❌ Ingresa un número válido de cuentas.")
            return

        monto = cantidad * PRECIO_POR_CUENTA
        pid = pedido_insert(user.id, user.username or "", cantidad, monto)
        user_pending_orders[user.id] = pid
        context.user_data["awaiting_cantidad"] = False

        await update.message.reply_text(
            f"Ok ✅\nEl precio por cada cuenta es {PRECIO_POR_CUENTA} soles.\n"
            f"El monto total a enviar es: {monto} soles.\n\n"
            "📸 Envía ahora la captura del pago por Yape para continuar."
        )

    else:
        await update.message.reply_text("Usa el botón Recargar para iniciar un pedido.")

async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in user_pending_orders:
        await update.message.reply_text("❌ No tienes un pedido pendiente.")
        return

    pid = user_pending_orders[user.id]
    file_id = update.message.photo[-1].file_id
    file = await context.bot.get_file(file_id)
    url = file.file_path

    pedido_set_comprobante(pid, url)

    # Notificar al admin
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Confirmar", callback_data=f"aprobar:{pid}:{user.id}")],
        [InlineKeyboardButton("❌ Rechazar", callback_data=f"rechazar:{pid}:{user.id}")]
    ])

    await context.bot.send_photo(
        chat_id=ADMIN_ID,
        photo=file_id,
        caption=f"📢 Nuevo pedido\nUsuario: @{user.username}\nCuentas: pendiente\nPedido ID: {pid}",
        reply_markup=kb
    )

    await update.message.reply_text("📩 Hemos recibido tu comprobante. El admin verificará tu pago pronto.")

async def on_admin_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    data = q.data.split(":")
    action, pid, uid = data[0], data[1], int(data[2])

    if action == "aprobar":
        # Buscar pedido para sumar créditos
        r = supabase.table("pedidos").select("cantidad").eq("id", pid).limit(1).execute()
        if r.data:
            cantidad = int(r.data[0]["cantidad"])
            new_total = user_add_credits(uid, cantidad)
            pedido_set_estado(pid, "aprobado")
            await context.bot.send_message(chat_id=uid, text=f"✅ Pago aprobado. Créditos añadidos: {cantidad}. Saldo total: {new_total}")
            await q.message.edit_caption(q.message.caption + "\n\n✅ APROBADO")
    elif action == "rechazar":
        pedido_set_estado(pid, "rechazado")
        await context.bot.send_message(chat_id=uid, text="❌ Tu pago fue rechazado. Verifica y vuelve a intentarlo.")
        await q.message.edit_caption(q.message.caption + "\n\n❌ RECHAZADO")

# -------------------- REGISTROS --------------------
tg_app.add_handler(CommandHandler("start", cmd_start))
tg_app.add_handler(CallbackQueryHandler(on_button, pattern="^recargar$"))
tg_app.add_handler(CallbackQueryHandler(on_admin_action, pattern="^(aprobar|rechazar):"))
tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
tg_app.add_handler(MessageHandler(filters.PHOTO, on_photo))

# -------------------- MAIN --------------------
def create_app():
    def _start_tg():
        tg_app.run_polling()

    threading.Thread(target=_start_tg, daemon=True).start()
    return None  # Railway espera un server, puedes usar Flask si necesitas healthcheck

if __name__ == "__main__":
    tg_app.run_polling()
