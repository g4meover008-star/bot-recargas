import os, io, uuid, asyncio, logging, qrcode
from datetime import datetime

from flask import Flask, request, jsonify
from mercadopago import SDK

# ====== Telegram con aiogram (no usa httpx) ======
from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, BufferedInputFile

# ====== Supabase con httpx >= 0.27 ======
from httpx import Client as HttpxClient
from supabase import create_client, Client, ClientOptions

# =============== LOG =================
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("recargas")

# =============== ENV =================
TG_BOT_TOKEN = os.getenv("TG_RECHARGE_BOT_TOKEN", "")     # token del BOT DE RECARGAS
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_ANON_KEY") or os.getenv("SUPABASE_API_KEY") or ""
MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN", "")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "")        # ej: https://web-production-xxxx.up.railway.app
CURRENCY = os.getenv("CURRENCY", "PEN")
PRICE_PER_CREDIT = float(os.getenv("PRICE_PER_CREDIT", "1"))  # 1 sol = 1 crédito por defecto

if not (TG_BOT_TOKEN and SUPABASE_URL and SUPABASE_KEY and MP_ACCESS_TOKEN and PUBLIC_BASE_URL):
    raise SystemExit(
        "Faltan variables: TG_RECHARGE_BOT_TOKEN, SUPABASE_URL, SUPABASE_ANON_KEY/API_KEY, "
        "MP_ACCESS_TOKEN, PUBLIC_BASE_URL"
    )

# =============== CLIENTES ================
# Supabase con httpx (>=0.27) para evitar el error de 'proxy'
http_client = HttpxClient(timeout=30.0)
supabase: Client = create_client(
    SUPABASE_URL,
    SUPABASE_KEY,
    options=ClientOptions(http_client=http_client)
)

mp = SDK(MP_ACCESS_TOKEN)

# Telegram (aiogram)
bot = Bot(TG_BOT_TOKEN, parse_mode="HTML")
dp = Dispatcher()
rt = Router()
dp.include_router(rt)

# Flask (webhook Mercado Pago)
app = Flask(__name__)

# =============== DB HELPERS ===============
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
            "created_at": datetime.utcnow().isoformat()
        },
        on_conflict="id"
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
    # historial (opcional)
    try:
        supabase.table("creditos_historial").insert({
            "usuario_id": str(user_id),
            "delta": to_add,
            "motivo": "recarga_mp",
            "hecho_por": "mercado_pago"
        }).execute()
    except Exception:
        pass
    return to_add, new_value

# =============== MP HELPERS ===============
def mp_create_preference(pago_id: str, amount: float):
    pref = {
        "items": [{
            "title": "Recarga de créditos",
            "quantity": 1,
            "currency_id": CURRENCY,
            "unit_price": float(amount)
        }],
        "external_reference": pago_id,
        "notification_url": f"{PUBLIC_BASE_URL}/mp/webhook"
    }
    resp = mp.preference().create(pref)
    return resp["response"]["id"], resp["response"]["init_point"]

def mp_get_payment(payment_id: str):
    return mp.payment().get(payment_id)

# =============== QR ===============
def build_qr_png_bytes(url: str) -> bytes:
    img = qrcode.make(url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()

# =============== TELEGRAM HANDLERS (aiogram) ===============
@rt.message(F.text.regexp(r"^/start"))
async def cmd_start(msg: Message):
    await msg.answer(
        "🤖 Bot de Recargas\n"
        "Usa <code>/recargar &lt;monto&gt;</code>\n\n"
        "Ejemplos:\n"
        "/recargar 5  (5 soles)\n"
        "/recargar 10 (10 soles)\n\n"
        "Recibirás un link de pago (y QR opcional). Cuando se apruebe, "
        "acreditaré automáticamente tus créditos."
    )

@rt.message(F.text.regexp(r"^/recargar(\s+.+)?"))
async def cmd_recargar(msg: Message):
    parts = (msg.text or "").strip().split()
    if len(parts) != 2:
        await msg.reply("Uso: /recargar <monto_en_soles>\nEj: /recargar 5")
        return

    try:
        amount = float(parts[1])
        if amount <= 0:
            raise ValueError()
    except Exception:
        await msg.reply("Monto inválido. Ej: /recargar 5")
        return

    pago_id = str(uuid.uuid4())
    pref_id, init_point = mp_create_preference(pago_id, amount)
    pagos_upsert(pago_id, str(msg.from_user.id), msg.from_user.username or "", amount, pref_id, init_point)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Pagar ahora", url=init_point)],
        [InlineKeyboardButton(text="🧾 Ver QR", callback_data=f"qr:{pago_id}")]
    ])
    await msg.reply(
        f"🔗 Tu link de pago por {amount:.2f} {CURRENCY} está listo.\n"
        f"ID de pedido: <code>{pago_id}</code>\n\n"
        "Cuando el pago se apruebe, acreditaré los créditos automáticamente. "
        "Si prefieres QR, pulsa “Ver QR”.",
        reply_markup=kb
    )

@rt.callback_query(F.data.startswith("qr:"))
async def on_qr_callback(q: CallbackQuery):
    pago_id = q.data.split(":", 1)[1]
    row = pagos_get(pago_id)
    if not row:
        await q.message.answer("No encuentro ese pedido.")
        await q.answer()
        return

    init_point = row.get("init_point")
    if not init_point:
        await q.message.answer("No tengo el link de pago aún.")
        await q.answer()
        return

    png = build_qr_png_bytes(init_point)
    await q.message.answer_photo(BufferedInputFile(png, filename="qr.png"), caption="Escanea para pagar.")
    await q.answer()

async def notify_user(user_id: int, amount_paid: float, added: int, new_total: int):
    try:
        text = (
            "✅ <b>Recarga acreditada</b>\n"
            f"Pago: {amount_paid:.2f} {CURRENCY}\n"
            f"Créditos añadidos: <b>{added}</b>\n"
            f"Saldo actual: <b>{new_total}</b>"
        )
        await bot.send_message(user_id, text)
    except Exception:
        log.warning("No pude notificar al usuario.")

# =============== FLASK (WEB) ===============
@app.get("/health")
def health():
    return "ok", 200

@app.post("/mp/webhook")
def mp_webhook():
    """
    Mercado Pago envía: { "type":"payment", "data":{"id":"<payment_id>"} }
    """
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

        if pedido.get("status") == "aprobado":
            return jsonify({"status": "already processed"}), 200

        if status == "approved":
            pagos_set_status(ext_ref, "aprobado", payment_id)
            user_id = int(pedido["user_id"])
            added, new_total = user_add_credits(str(user_id), amount)

            asyncio.get_event_loop().create_task(
                notify_user(user_id, amount, added, new_total)
            )
            return jsonify({"status": "ok"}), 200

        elif status in ("rejected", "cancelled", "refunded", "charged_back"):
            pagos_set_status(ext_ref, status, payment_id)
            return jsonify({"status": status}), 200
        else:
            pagos_set_status(ext_ref, status, payment_id)   # pending / in_process / etc.
            return jsonify({"status": status}), 200

    except Exception as e:
        log.exception("Error en webhook")
        return jsonify({"error": str(e)}), 500

# =============== MAIN ===============
if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))

    async def run_all():
        # Telegram polling en segundo plano
        asyncio.create_task(dp.start_polling(bot))
        # Servidor HTTP para Railway
        from waitress import serve
        serve(app, host="0.0.0.0", port=port)

    asyncio.run(run_all())
