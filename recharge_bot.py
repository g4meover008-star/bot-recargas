# recharge_bot.py
# -*- coding: utf-8 -*-

import os
import io
import uuid
import json
import time
import logging
import threading
import asyncio
from datetime import datetime

import requests
from flask import Flask, request

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ============ LOGGING ============
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("recargas")

# ============ ENV ============

TG_BOT_TOKEN     = os.getenv("TG_RECHARGE_BOT_TOKEN", "")
ADMIN_CHAT_ID    = int(os.getenv("ADMIN_CHAT_ID", "0"))
BRAND_NAME       = os.getenv("BRAND_NAME", "Mi Marca")
PRICE_PER_CREDIT = float(os.getenv("PRICE_PER_CREDIT", "1"))
SUPABASE_URL     = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY     = os.getenv("SUPABASE_API_KEY", "")
YAPE_QR_URL      = os.getenv("YAPE_QR_URL", "")
# --- Límites de compra ---
MIN_QTY = 2           # compra mínima en cuentas
# MAX_QTY = 100       # (opcional) tope máximo


if not all([TG_BOT_TOKEN, ADMIN_CHAT_ID, SUPABASE_URL, SUPABASE_KEY, YAPE_QR_URL]):
    raise SystemExit(
        "Faltan variables: TG_RECHARGE_BOT_TOKEN, ADMIN_CHAT_ID, SUPABASE_URL, "
        "SUPABASE_API_KEY, YAPE_QR_URL"
    )

log.info("Recargas %s iniciando…", BRAND_NAME)
log.info("YAPE_QR_URL: %s", "definido" if YAPE_QR_URL else "no definido")

# ============ Supabase (REST) ============

SB_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
}

# === PRECIOS POR TRAMO (PEN) ===
PRICE_TIER_1_9   = float(os.getenv("PRICE_TIER_1_9", "27.00"))   # 1 a 9 cuentas
PRICE_TIER_10_UP = float(os.getenv("PRICE_TIER_10_UP", "26.00")) # 10 o más cuentas

def sb_get_user(telegram_id: int):
    """Trae info del usuario desde 'usuarios' incluyendo cuentas_asignadas."""
    return sb_select_one(
        "usuarios",
        {"telegram_id": str(telegram_id)},
        "telegram_id, username, creditos, cuentas_asignadas"
    )

def get_price_for_user(telegram_id: int) -> float:
    """
    Precio por cuenta según 'usuarios.cuentas_asignadas':
    - 10 o más -> 26.00
    - 1 a 9 (y 0 / null) -> 27.00
    """
    u = sb_get_user(telegram_id)
    asignadas = int((u or {}).get("cuentas_asignadas") or 0)
    return PRICE_TIER_10_UP if asignadas >= 10 else PRICE_TIER_1_9


def sb_select_one(table: str, filters: dict, columns: str = "*"):
    """GET /rest/v1/{table}?col=eq.value&select=*  -> dict | None"""
    params = {"select": columns}
    for k, v in filters.items():
        params[k] = f"eq.{v}"
    try:
        r = requests.get(f"{SUPABASE_URL}/rest/v1/{table}", headers=SB_HEADERS, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        return data[0] if data else None
    except Exception as e:
        log.warning("Supabase select_one %s error: %s", table, e)
        return None

def sb_insert(table: str, row: dict):
    try:
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/{table}",
            headers={**SB_HEADERS, "Prefer": "return=representation"},
            json=row,
            timeout=10,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning("Supabase insert %s error: %s", table, e)
        return None

def sb_patch(table: str, filters: dict, patch: dict):
    params = {}
    for k, v in filters.items():
        params[k] = f"eq.{v}"
    try:
        r = requests.patch(
            f"{SUPABASE_URL}/rest/v1/{table}",
            headers={**SB_HEADERS, "Prefer": "return=representation"},
            params=params,
            json=patch,
            timeout=10,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning("Supabase patch %s error: %s", table, e)
        return None

def sb_add_credits(telegram_id: int, to_add: int) -> int:
    """Suma créditos al usuario; retorna nuevo total (o el anterior si falla)."""
    user = sb_select_one("usuarios", {"telegram_id": str(telegram_id)}, "creditos,telegram_id")
    current = int(user["creditos"]) if (user and user.get("creditos") is not None) else 0
    new_total = current + int(to_add)

    if user:
        sb_patch("usuarios", {"telegram_id": str(telegram_id)}, {"creditos": new_total})
    else:
        sb_insert("usuarios", {"telegram_id": str(telegram_id), "creditos": new_total})

    # Historial (no bloqueante)
    try:
        sb_insert(
            "creditos_historial",
            {
                "usuario_id": str(telegram_id),
                "delta": int(to_add),
                "motivo": "recarga_aprobada",
                "hecho_por": "admin",
            },
        )
    except Exception:
        pass

    return new_total

# ============ Telegram App ============

app_tg = Application.builder().token(TG_BOT_TOKEN).build()

# Keys de user_data
UD_AWAIT_QTY   = "await_qty"
UD_ORDER       = "order"         # dict con {id, qty, amount}
UD_AWAIT_PROOF = "await_proof"

# ========= Helpers de UI =========

def kb_home():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 Recargar", callback_data="recargar")],
        [InlineKeyboardButton("💼 Mis créditos", callback_data="saldo")],
        [InlineKeyboardButton("❓ Ayuda", callback_data="ayuda")],
    ])

def kb_cancel():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Cancelar solicitud", callback_data="cancel")],
    ])

def kb_admin(order_id: str, user_id: int, qty: int):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Aprobar", callback_data=f"approve:{order_id}:{user_id}:{qty}"),
            InlineKeyboardButton("⛔ Rechazar", callback_data=f"reject:{order_id}:{user_id}"),
        ]
    ])

# ===== NEW: util para borrar un mensaje con botón =====
async def _delete_button_message(update_or_query):
    """Borra el mensaje que contiene el botón que se pulsó (si existe)."""
    try:
        if hasattr(update_or_query, "callback_query") and update_or_query.callback_query:
            msg = update_or_query.callback_query.message
        else:
            msg = update_or_query.effective_message
        if msg:
            await msg.delete()
    except Exception:
        pass

# ========= Handlers =========

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        f"👋 Bienvenido a <b>{BRAND_NAME}</b>.\n\n"
        "Selecciona una opción:"
    )
    await update.effective_chat.send_message(text, reply_markup=kb_home(), parse_mode="HTML")

async def on_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""

    # NEW: borra el mensaje con el botón antes de mostrar el siguiente paso
    await _delete_button_message(update)

    if data == "recargar":
        context.user_data[UD_AWAIT_QTY] = True
        context.user_data.pop(UD_ORDER, None)
        context.user_data.pop(UD_AWAIT_PROOF, None)

        user_id = update.effective_user.id
        unit_price = get_price_for_user(user_id)

        await q.message.chat.send_message(
            "Indica cuántas <b>cuentas</b> deseas comprar.\n"
            f"Precio por cuenta (según tus cuentas asignadas): <b>{unit_price:.2f} PEN</b>.",
            parse_mode="HTML"
        )

    elif data == "saldo":
        # muestra créditos actuales (puedes dejarlo así o usar sb_get_user)
        user_id = update.effective_user.id
        user = sb_select_one("usuarios", {"telegram_id": str(user_id)}, "creditos")
        cred = int(user["creditos"]) if (user and user.get("creditos") is not None) else 0
        await q.message.chat.send_message(f"💼 Tus créditos: <b>{cred}</b>", parse_mode="HTML")

    elif data == "ayuda":
        await q.message.chat.send_message(
            "1) Pulsa <b>Recargar</b> y escribe la cantidad de cuentas.\n"
            "2) Paga el monto exacto usando el QR de Yape.\n"
            "3) Envíame la <b>captura del pago</b> en este chat.\n"
            "4) Un admin aprobará y se acreditarán tus créditos.",
            parse_mode="HTML"
        )

    elif data == "cancel":
        context.user_data.clear()
        await q.message.chat.send_message("✅ Solicitud cancelada. Vuelve a empezar con /start.")

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Capta la cantidad cuando el usuario está en modo 'await_qty'."""
    if not context.user_data.get(UD_AWAIT_QTY):
        await cmd_start(update, context)   # mostrar el mismo mensaje que /start
        return

    txt = (update.message.text or "").strip()

    # Para que el linter no se queje
    qty = 0
    try:
        qty = int(txt)
        if qty <= 0:
            raise ValueError()
    except Exception:
        await update.message.reply_text("❗ Ingresa un número válido de cuentas. Ej: 2")
        return

    # --- MÍNIMO DE COMPRA ---
    if qty < MIN_QTY:
        await update.message.reply_text(
            f"La compra mínima es de {MIN_QTY} cuentas. Ingresa un número mayor o igual a {MIN_QTY}."
        )
        return

    # (opcional) máximo:
    # if qty > MAX_QTY:
    #     await update.message.reply_text(f"El máximo permitido es {MAX_QTY} cuentas.")
    #     return

    user_id = update.effective_user.id
    unit_price = get_price_for_user(user_id)
    amount = qty * unit_price

    order_id = str(uuid.uuid4())[:8]

    # Guarda orden en memoria y en DB (no bloqueante)
    context.user_data[UD_ORDER] = {
    "id": order_id,
    "qty": qty,
    "amount": amount,
    "unit_price": unit_price,   # <-- guardamos el precio aplicado
}

    context.user_data[UD_AWAIT_PROOF] = True
    context.user_data[UD_AWAIT_QTY] = False

    try:
        sb_insert("pagos", {
            "id": order_id,
            "user_id": str(update.effective_user.id),
            "username": update.effective_user.username or "",
            "amount": float(amount),
            "qty": int(qty),
            "status": "pendiente",
            "created_at": datetime.utcnow().isoformat()
        })
    except Exception:
        pass

    caption = (
    f"<b>Pedido {order_id}</b>\n"
    f"Precio por cuenta: <b>{unit_price:.2f} PEN</b>\n"
    f"Créditos/Cuentas: <b>{qty}</b>\n"
    f"Importe total: <b>{amount:.2f} PEN</b>\n\n"
    "1) Escanea o abre el QR de Yape.\n"
    "2) Paga el monto exacto.\n"
    "3) Envíame la <b>captura del pago</b> a este chat."
)


    # NEW: como aquí no hay botón, no borramos nada.
    if YAPE_QR_URL.lower().startswith(("http://", "https://")):
        await update.message.reply_photo(
            YAPE_QR_URL,
            caption=caption,
            parse_mode="HTML",
            reply_markup=kb_cancel()
        )
    else:
        await update.message.reply_text(caption, parse_mode="HTML", reply_markup=kb_cancel())

async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Recibe la captura y la manda al admin con botones de aprobar/rechazar."""
    if not context.user_data.get(UD_AWAIT_PROOF) or not context.user_data.get(UD_ORDER):
        await cmd_start(update, context)   # responder como /start si no está en flujo
        return

    order = context.user_data[UD_ORDER]
    unit_price = order.get("unit_price")
    user = update.effective_user
    photo = update.message.photo[-1]  # mejor calidad
    file_id = photo.file_id

    amount = order["amount"]
    qty = order["qty"]
    order_id = order["id"]

    # Notifica al admin
    cap_admin = (
    f"📥 <b>Pago recibido</b>\n"
    f"Usuario: <code>{user.id}</code> @{user.username}\n"
    f"Pedido: <code>{order_id}</code>\n"
    f"Precio unitario: <b>{unit_price:.2f} PEN</b>\n"  # <-- nueva línea
    f"Importe: <b>{amount:.2f} PEN</b>\n"
    f"Créditos solicitados: <b>{qty}</b>"
)

    await context.bot.send_photo(
        chat_id=ADMIN_CHAT_ID,
        photo=file_id,
        caption=cap_admin,
        parse_mode="HTML",
        reply_markup=kb_admin(order_id, user.id, qty)
    )

    await update.message.reply_text(
        "✅ Captura recibida. Un administrador revisará tu pago en breve."
    )
    # opcional: context.user_data.clear()

async def on_admin_actions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Approve/Reject desde el admin."""
    q = update.callback_query
    await q.answer()
    data = q.data or ""

    if data.startswith("approve:"):
        _, order_id, user_id_str, qty_str = data.split(":")
        user_id = int(user_id_str)
        qty = int(qty_str)

        try:
            sb_patch("pagos", {"id": order_id}, {"status": "aprobado", "updated_at": datetime.utcnow().isoformat()})
        except Exception:
            pass

        new_total = sb_add_credits(user_id, qty)

        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    "✅ <b>Pago verificado</b>.\n"
                    f"Se añadieron <b>{qty}</b> créditos.\n"
                    f"Saldo actual: <b>{new_total}</b>"
                ),
                parse_mode="HTML"
            )
        except Exception:
            log.warning("No pude notificar al usuario %s", user_id)

        await q.edit_message_caption(
            caption=q.message.caption + "\n\n✅ Aprobado y créditos acreditados."
        )

    elif data.startswith("reject:"):
        _, order_id, user_id_str = data.split(":")
        user_id = int(user_id_str)

        try:
            sb_patch("pagos", {"id": order_id}, {"status": "rechazado", "updated_at": datetime.utcnow().isoformat()})
        except Exception:
            pass

        try:
            await context.bot.send_message(
                chat_id=user_id,
                text="⛔ Tu pago fue rechazado. Si crees que es un error, contáctanos."
            )
        except Exception:
            pass

        await q.edit_message_caption(
            caption=q.message.caption + "\n\n⛔ Rechazado por el admin."
        )

# ============ Flask (health) ============

app_flask = Flask(__name__)

@app_flask.route("/health", methods=["GET"])
def health():
    return "ok", 200

# ============ Arranque ============

def run_http():
    from waitress import serve
    port = int(os.getenv("PORT", "8080"))
    log.info("HTTP escuchando en 0.0.0.0:%s", port)
    serve(app_flask, host="0.0.0.0", port=port)

def run_bot():
    """
    Corre Telegram en un hilo con su propio event loop (Python 3.12+).
    stop_signals=None evita registrar señales fuera del hilo principal.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    app_tg.run_polling(
        allowed_updates=Update.ALL_TYPES,
        close_loop=True,
        stop_signals=None
    )

# Fallback: si envían stickers, audios, documentos, contactos, etc.
async def on_anything_else(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get(UD_AWAIT_QTY) and not context.user_data.get(UD_AWAIT_PROOF):
        await cmd_start(update, context)


def register_handlers():
    app_tg.add_handler(CommandHandler("start", cmd_start))
    app_tg.add_handler(CallbackQueryHandler(on_buttons, pattern="^(recargar|saldo|ayuda|cancel)$"))
    app_tg.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app_tg.add_handler(CallbackQueryHandler(on_admin_actions, pattern="^(approve:|reject:)"))
    # textos (cantidad) cuando está esperando o menú si no
    app_tg.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    # NUEVO: fallback para cualquier otro tipo de mensaje
    app_tg.add_handler(MessageHandler(~filters.COMMAND & ~filters.TEXT & ~filters.PHOTO, on_anything_else))

register_handlers()

if __name__ == "__main__":
    # HTTP en un hilo
    th = threading.Thread(target=run_http, name="http", daemon=True)
    th.start()

    # Bot en otro hilo (con su propio event loop)
    run_bot()
