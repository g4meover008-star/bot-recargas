import os, hmac, hashlib, json, logging
from fastapi import FastAPI, Request, Response
from fastapi.responses import PlainTextResponse
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from telegram.constants import ParseMode
from telegram import Update
from supabase import create_client

# ========= Config =========
BOT_TOKEN = os.getenv("RECHARGE_BOT_TOKEN", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_API_KEY", "")
NOW_API_KEY = os.getenv("NOW_API_KEY", "")  # API Key o IPN Secret
PRICE_USDT = float(os.getenv("PRICE_USDT", "6.5"))  # precio 1 crédito
MIN_CREDITOS = int(os.getenv("MIN_CREDITOS", "10"))
WEBHOOK_BASE = os.getenv("WEBHOOK_BASE", "")  # https://xxxx.up.railway.app

if not (BOT_TOKEN and SUPABASE_URL and SUPABASE_KEY and NOW_API_KEY and WEBHOOK_BASE):
    raise SystemExit("Faltan variables de entorno.")

log = logging.getLogger("recargas")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# DB
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# ========= Telegram =========
app_tg = ApplicationBuilder().token(BOT_TOKEN).build()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Bienvenido al bot de recargas.\n"
        f"Tarifa: <b>{PRICE_USDT:.2f} USDT</b> por crédito.\n"
        f"Mínimo: <b>{MIN_CREDITOS}</b> créditos.\n\n"
        "Envía un número (cantidad de créditos) para generar el enlace de pago.",
        parse_mode=ParseMode.HTML
    )

async def on_number(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text.isdigit():
        return
    qty = int(text)
    if qty < MIN_CREDITOS:
        await update.message.reply_text(f"El mínimo es {MIN_CREDITOS}.")
        return
    amount = qty * PRICE_USDT

    # Creamos "orden" simple en DB (opcional, útil para conciliar)
    uid = str(update.effective_user.id)
    username = update.effective_user.username or f"user_{uid}"
    payload = {"uid": uid, "username": username, "qty": qty, "amount": amount}
    try:
        supabase.table("creditos_historial").insert({
            "usuario_id": uid, "delta": 0, "motivo": "orden_pendiente", "hecho_por": "recargas_bot"
        }).execute()
    except Exception:
        pass

    # Enlace simple a NOWPayments (red USDT-TRC20 por defecto)
    # En producción lo ideal es crear invoice via API y obtener payment_url.
    # Para simplificar, dejamos instrucciones + un "id" para que el pagador lo ponga en el memo/comentario.
    order_id = f"{uid}-{qty}"
    pay_url = f"https://nowpayments.io/payment?amount={amount:.2f}&currency=usdttrc20&orderId={order_id}"

    await update.message.reply_text(
        f"🧾 Vas a comprar <b>{qty}</b> créditos.\n"
        f"Total: <b>{amount:.2f} USDT</b>\n\n"
        f"👉 <a href='{pay_url}'>Pagar ahora</a>\n\n"
        f"Tras el pago, el sistema acreditará automáticamente.",
        parse_mode=ParseMode.HTML, disable_web_page_preview=True
    )

app_tg.add_handler(CommandHandler("start", start))
app_tg.add_handler(CommandHandler("ayuda", start))
app_tg.add_handler(CommandHandler("help", start))
app_tg.add_handler(CommandHandler("creditos", start))
app_tg.add_handler(CommandHandler("saldo", start))
app_tg.add_handler(CommandHandler("precio", start))
app_tg.add_handler(CommandHandler("buy", start))
app_tg.add_handler(CommandHandler("recargar", start))
app_tg.add_handler(CommandHandler("pagar", start))
app_tg.add_handler(CommandHandler("inicio", start))
app_tg.add_handler(CommandHandler("menu", start))

# Handler genérico: si envían un número
from telegram.ext import MessageHandler, filters
app_tg.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), on_number))

# ========= IPN (webhook) =========
api = FastAPI()

def _valid_signature(raw: bytes, signature: str) -> bool:
    mac = hmac.new(NOW_API_KEY.encode("utf-8"), msg=raw, digestmod=hashlib.sha256).hexdigest()
    return hmac.compare_digest(mac, (signature or "").lower())

@api.post("/ipn")
async def ipn(request: Request):
    raw = await request.body()
    sig = request.headers.get("x-nowpayments-sig", "")
    if not _valid_signature(raw, sig):
        log.warning("Firma IPN inválida.")
        return PlainTextResponse("bad signature", status_code=400)

    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        return PlainTextResponse("bad json", status_code=400)

    # NOWPayments manda varios estados; acreditamos sólo cuando está finalizado
    status = str(data.get("payment_status") or "").lower()
    amount_received = float(data.get("actually_paid", 0.0) or 0.0)
    order_id = str(data.get("order_id") or "")

    log.info(f"IPN status={status} order={order_id} paid={amount_received}")

    if status not in ("finished", "confirmed"):
        return PlainTextResponse("ok", status_code=200)

    # order_id = "<uid>-<qty>"
    try:
        uid, qty = order_id.split("-", 1)
        qty = int(qty)
    except Exception:
        # si no se pudo, intentamos mapear por amount (riesgoso, pero de emergencia)
        return PlainTextResponse("order parse error", status_code=200)

    # Acreditar en Supabase
    try:
        # obtener saldo actual
        r = supabase.table("usuarios").select("creditos, username").eq("telegram_id", uid).execute()
        current = int(r.data[0]["creditos"]) if r.data and r.data[0].get("creditos") is not None else 0
        supabase.table("usuarios").update({"creditos": current + qty}).eq("telegram_id", uid).execute()
        # historial
        supabase.table("creditos_historial").insert({
            "usuario_id": uid, "delta": qty, "motivo": "recarga_nowpayments", "hecho_por": "ipn"
        }).execute()
    except Exception as e:
        log.error(f"DB error: {e}")

    # (Opcional) notificar al usuario por Telegram
    try:
        await app_tg.bot.send_message(chat_id=int(uid),
            text=f"✅ Pago verificado. Se añadieron <b>{qty}</b> créditos.",
            parse_mode=ParseMode.HTML)
    except Exception:
        pass

    return PlainTextResponse("ok", status_code=200)

# ========= Arranque conjunto =========
@app_tg.post_init
async def _after_init(app, *args, **kwargs):
    # quita cualquier webhook previo para usar long-polling
    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        pass
    log.info("Bot listo.")

def main():
    import uvicorn, threading
    # Arranca Telegram (polling) en un hilo
    threading.Thread(target=app_tg.run_polling, kwargs={"allowed_updates": None}, daemon=True).start()
    # Arranca FastAPI para el webhook /ipn
    uvicorn.run(api, host="0.0.0.0", port=int(os.getenv("PORT", "8080")))

if __name__ == "__main__":
    main()
