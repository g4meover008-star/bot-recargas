import os, io, uuid, asyncio, logging
from datetime import datetime
from typing import Optional

import requests
from flask import Flask

from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton, Bot as TGBot
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)

# ================= LOGGING =================
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("recargas")

# ================= ENV =================
TG_BOT_TOKEN     = os.getenv("TG_RECHARGE_BOT_TOKEN", "")
ADMIN_CHAT_ID    = int(os.getenv("ADMIN_CHAT_ID", "0") or "0")       # chat id del admin
ADMIN_BOT_TOKEN  = os.getenv("ADMIN_BOT_TOKEN", "")                  # opcional; si no, se usa el mismo bot
BRAND_NAME       = os.getenv("BRAND_NAME", "Tu Marca")
YAPE_QR_URL      = os.getenv("YAPE_QR_URL", "")
PRICE_PER_CREDIT = float(os.getenv("PRICE_PER_CREDIT", "1"))

SUPABASE_URL     = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY     = os.getenv("SUPABASE_API_KEY") or os.getenv("SUPABASE_ANON_KEY") or ""

if not (TG_BOT_TOKEN and ADMIN_CHAT_ID and SUPABASE_URL and SUPABASE_KEY):
    raise SystemExit(
        "Faltan variables: TG_RECHARGE_BOT_TOKEN, ADMIN_CHAT_ID, SUPABASE_URL, SUPABASE_API_KEY/ANON_KEY"
    )

# ================= SUPABASE REST HELPERS =================
SB_REST = f"{SUPABASE_URL.rstrip('/')}/rest/v1"
SB_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
}
SB_TIMEOUT = 12

def sb_insert(table: str, data: dict):
    r = requests.post(
        f"{SB_REST}/{table}",
        headers={**SB_HEADERS, "Prefer": "return=representation"},
        json=data, timeout=SB_TIMEOUT
    )
    r.raise_for_status()
    return r.json()[0] if r.text else None

def sb_patch(table: str, where: str, data: dict):
    # where ejemplo: "id=eq.abc123"  | "telegram_id=eq.12345"
    r = requests.patch(
        f"{SB_REST}/{table}?{where}",
        headers={**SB_HEADERS, "Prefer": "return=representation"},
        json=data, timeout=SB_TIMEOUT
    )
    r.raise_for_status()
    return r.json()[0] if r.text else None

def sb_select_one(table: str, where: str, select="*", order: Optional[str] = None):
    url = f"{SB_REST}/{table}?{where}"
    params = {"select": select}
    if order:
        url += f"&order={order}"
    r = requests.get(url, headers=SB_HEADERS, params=params, timeout=SB_TIMEOUT)
    r.raise_for_status()
    arr = r.json()
    return arr[0] if arr else None

# Usuario
def ensure_user(user_id: int, username: str):
    u = sb_select_one("usuarios", f"telegram_id=eq.{user_id}", select="telegram_id,username,creditos")
    if not u:
        return sb_insert("usuarios", {
            "telegram_id": str(user_id),
            "username": username or "",
            "creditos": 0
        })
    return u

def get_user_credits(user_id: int) -> int:
    u = sb_select_one("usuarios", f"telegram_id=eq.{user_id}", select="creditos")
    return int(u["creditos"]) if u and u.get("creditos") is not None else 0

def add_credits(user_id: int, delta: int, motivo="recarga_yape") -> int:
    u = ensure_user(user_id, "")
    current = int(u.get("creditos", 0))
    new_val = current + int(delta)
    sb_patch("usuarios", f"telegram_id=eq.{user_id}", {"creditos": new_val, "updated_at": datetime.utcnow().isoformat()})
    try:
        sb_insert("creditos_historial", {
            "usuario_id": str(user_id),
            "delta": int(delta),
            "motivo": motivo,
            "hecho_por": "admin"
        })
    except Exception:
        pass
    return new_val

# Pagos
def create_pago(pago_id: str, user_id: int, username: str, amount: float):
    return sb_insert("pagos", {
        "id": pago_id,
        "user_id": str(user_id),
        "username": username or "",
        "amount": float(amount),
        "status": "pendiente",
        "created_at": datetime.utcnow().isoformat()
    })

def get_pago(pago_id: str):
    return sb_select_one("pagos", f"id=eq.{pago_id}")

def last_pending_for_user(user_id: int):
    return sb_select_one(
        "pagos",
        f"user_id=eq.{user_id}&status=eq.pendiente",
        order="created_at.desc",
        select="*"
    )

def set_pago_status(pago_id: str, new_status: str, extra: Optional[dict] = None):
    data = {"status": new_status, "updated_at": datetime.utcnow().isoformat()}
    if extra:
        data.update(extra)
    return sb_patch("pagos", f"id=eq.{pago_id}", data)

# ================= TELEGRAM =================
app_flask = Flask(__name__)
tg_app = ApplicationBuilder().token(TG_BOT_TOKEN).build()

def calc_credits(amount: float) -> int:
    # 1 sol = 1 crédito por defecto (configurable por PRICE_PER_CREDIT)
    return int(round(float(amount) / max(PRICE_PER_CREDIT, 0.0001)))

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"👋 Bienvenido a <b>{BRAND_NAME}</b>.\n"
        "Este bot te permite <b>recargar créditos con Yape</b>.\n\n"
        "Comandos:\n"
        "• /recargar <monto> — Ej: /recargar 5\n"
        "• /saldo — ver tus créditos\n"
        "• /cancel — cancelar solicitud pendiente",
        parse_mode="HTML"
    )

async def cmd_saldo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    credits = get_user_credits(update.effective_user.id)
    await update.message.reply_text(f"💳 Tus créditos: <b>{credits}</b>", parse_mode="HTML")

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    row = last_pending_for_user(update.effective_user.id)
    if not row:
        await update.message.reply_text("No tienes solicitudes pendientes.")
        return
    set_pago_status(row["id"], "cancelado")
    await update.message.reply_text("✔️ Solicitud cancelada.")

async def cmd_recargar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if len(context.args) != 1:
        await update.message.reply_text("Uso: /recargar <monto>\nEj: /recargar 5")
        return
    try:
        amount = float(context.args[0])
        if amount <= 0:
            raise ValueError()
    except Exception:
        await update.message.reply_text("Monto inválido. Ej: /recargar 5")
        return

    # crea registro
    pago_id = uuid.uuid4().hex[:12]
    try:
        create_pago(pago_id, user.id, user.username or "", amount)
    except Exception as e:
        log.exception("No pude crear pago")
        await update.message.reply_text("Error creando la solicitud. Intenta de nuevo.")
        return

    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Cancelar solicitud", callback_data=f"cancel:{pago_id}")]]
    )
    caption = (
        f"🧾 Pedido <code>{pago_id}</code>\n"
        f"Importe: <b>{amount:.2f} PEN</b>\n"
        f"Créditos que recibirás: <b>{calc_credits(amount)}</b>\n\n"
        "1) Escanea o abre el QR de Yape (abajo).\n"
        "2) Paga el monto exacto.\n"
        "3) Envíame <b>la captura</b> del pago en este chat."
    )
    if YAPE_QR_URL:
        await update.message.reply_photo(
            YAPE_QR_URL, caption=caption, parse_mode="HTML", reply_markup=kb
        )
    else:
        await update.message.reply_text(caption, parse_mode="HTML", reply_markup=kb)

async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    row = last_pending_for_user(user.id)
    if not row:
        await update.message.reply_text("No encuentro una solicitud pendiente. Usa /recargar primero.")
        return

    # guardar file_id
    photo = update.message.photo[-1]
    file_id = photo.file_id
    try:
        set_pago_status(row["id"], "en_revision", {"capture_file_id": file_id})
    except Exception:
        pass

    # descargar y reenviar al admin (si hay ADMIN_BOT_TOKEN se usa ese bot; si no, el mismo)
    tmp = f"/tmp/{uuid.uuid4().hex}.jpg"
    try:
        tg_file = await context.bot.get_file(file_id)
        await tg_file.download_to_drive(tmp)
    except Exception as e:
        log.warning("No pude descargar la foto: %s", e)
        tmp = None

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Aprobar", callback_data=f"adm:ok:{row['id']}"),
            InlineKeyboardButton("❌ Rechazar", callback_data=f"adm:ko:{row['id']}"),
        ]
    ])
    caption = (
        f"🧾 <b>Solicitud de recarga</b>\n"
        f"ID: <code>{row['id']}</code>\n"
        f"Usuario: @{user.username or 'sin_usuario'} (ID {user.id})\n"
        f"Monto: <b>{row['amount']:.2f} PEN</b>\n"
        f"Créditos: <b>{calc_credits(row['amount'])}</b>"
    )

    try:
        if ADMIN_BOT_TOKEN:
            async with TGBot(ADMIN_BOT_TOKEN) as abot:
                if tmp and os.path.exists(tmp):
                    with open(tmp, "rb") as f:
                        await abot.send_photo(ADMIN_CHAT_ID, f, caption=caption, parse_mode="HTML", reply_markup=kb)
                else:
                    await abot.send_message(ADMIN_CHAT_ID, caption + "\n\n(No se pudo reenviar la imagen)", parse_mode="HTML", reply_markup=kb)
        else:
            if tmp and os.path.exists(tmp):
                with open(tmp, "rb") as f:
                    await context.bot.send_photo(ADMIN_CHAT_ID, f, caption=caption, parse_mode="HTML", reply_markup=kb)
            else:
                await context.bot.send_message(ADMIN_CHAT_ID, caption + "\n\n(No se pudo reenviar la imagen)", parse_mode="HTML", reply_markup=kb)
    finally:
        try:
            if tmp and os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass

    await update.message.reply_text("📸 Captura recibida. En breve el administrador la revisará.")

async def on_admin_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.from_user.id != ADMIN_CHAT_ID:
        await q.answer("Solo el admin puede usar estos botones.", show_alert=True)
        return

    data = (q.data or "").split(":")
    if len(data) != 3 or data[0] != "adm":
        return
    action, pago_id = data[1], data[2]

    row = get_pago(pago_id)
    if not row:
        await q.edit_message_caption(caption="(Registro no encontrado)")
        return
    if row["status"] == "aprobado":
        await q.answer("Ya estaba aprobado.")
        return
    if row["status"] == "rechazado":
        await q.answer("Ya estaba rechazado.")
        return

    if action == "ok":
        credits = calc_credits(row["amount"])
        new_total = add_credits(int(row["user_id"]), credits, motivo="recarga_yape")
        set_pago_status(pago_id, "aprobado", {"admin_id": str(ADMIN_CHAT_ID)})

        # notificar al usuario
        text_user = (
            "✅ <b>Recarga acreditada</b>\n"
            f"Monto: {row['amount']:.2f} PEN\n"
            f"Créditos añadidos: <b>{credits}</b>\n"
            f"Saldo actual: <b>{new_total}</b>"
        )
        try:
            await context.bot.send_message(chat_id=int(row["user_id"]), text=text_user, parse_mode="HTML")
        except Exception:
            pass

        await q.edit_message_caption(
            caption=f"✔️ APROBADO\nID {pago_id} — {row['amount']:.2f} PEN — +{credits} créditos",
            parse_mode="HTML"
        )
    else:
        set_pago_status(pago_id, "rechazado", {"admin_id": str(ADMIN_CHAT_ID)})
        try:
            await context.bot.send_message(chat_id=int(row["user_id"]),
                                           text="❌ Tu recarga fue rechazada. Si crees que es un error, contáctanos.")
        except Exception:
            pass
        await q.edit_message_caption(caption=f"❌ RECHAZADO\nID {pago_id}", parse_mode="HTML")

async def on_cancel_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = (q.data or "").split(":")
    if len(data) != 2 or data[0] != "cancel":
        return
    pago_id = data[1]
    set_pago_status(pago_id, "cancelado")
    await q.edit_message_caption(caption=f"🧾 Pedido {pago_id}\nEstado: CANCELADO")

# ================ REGISTRO HANDLERS ================
tg_app.add_handler(CommandHandler("start",   cmd_start))
tg_app.add_handler(CommandHandler("saldo",   cmd_saldo))
tg_app.add_handler(CommandHandler("cancel",  cmd_cancel))
tg_app.add_handler(CommandHandler("recargar", cmd_recargar))
tg_app.add_handler(MessageHandler(filters.PHOTO, on_photo))
tg_app.add_handler(CallbackQueryHandler(on_admin_action, pattern=r"^adm:"))
tg_app.add_handler(CallbackQueryHandler(on_cancel_button, pattern=r"^cancel:"))

# ================= FLASK =================
@app_flask.route("/health", methods=["GET"])
def health():
    return "ok", 200

# ================= MAIN (CORREGIDO) =================
import threading

if __name__ == "__main__":
    log.info("Recargas %s iniciando…", BRAND_NAME)
    log.info("YAPE_QR_URL: %s", "definido" if YAPE_QR_URL else "NO definido")
    port = int(os.getenv("PORT", "8080"))

    def run_http():
        """Servidor HTTP (Flask + Waitress) en un hilo separado."""
        from waitress import serve
        serve(app_flask, host="0.0.0.0", port=port)

    async def main():
        # 1) Levanta Flask en otro hilo para no bloquear el event loop
        threading.Thread(target=run_http, daemon=True).start()

        # 2) Inicia el bot sin bloquear el event loop
        await tg_app.initialize()
        await tg_app.start()
        await tg_app.updater.start_polling()

        log.info("Telegram bot en polling. Todo listo.")

        # 3) Mantén vivo el event loop
        await asyncio.Event().wait()

    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
