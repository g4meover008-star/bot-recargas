import os, io, uuid, asyncio, logging, qrcode
from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
from flask import Flask

# ========= CONFIG =========
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("yape")

TG_BOT_TOKEN = os.getenv("TG_RECHARGE_BOT_TOKEN", "")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://localhost:8080")  # Railway domain
YAPE_NUMBER = os.getenv("YAPE_NUMBER", "999999999")   # tu número de celular Yape
CURRENCY = os.getenv("CURRENCY", "PEN")

if not TG_BOT_TOKEN:
    raise SystemExit("Falta TG_RECHARGE_BOT_TOKEN")

bot = Bot(TG_BOT_TOKEN, parse_mode="HTML")
dp = Dispatcher()
rt = Router()
dp.include_router(rt)

app = Flask(__name__)

# ========= QR Helper =========
def build_qr_yape(amount: float) -> bytes:
    """
    Genera un QR simple con la info de Yape.
    En Yape oficial se genera desde la app, pero aquí 
    usamos un QR con el número + monto como referencia.
    """
    yape_url = f"yape://pay?phone={YAPE_NUMBER}&amount={amount:.2f}"
    img = qrcode.make(yape_url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()

# ========= HANDLERS =========
@rt.message(F.text.regexp(r"^/start"))
async def cmd_start(msg: Message):
    await msg.answer(
        "🤖 Recargas por Yape\n\n"
        "Usa /recargar <monto>\nEjemplo:\n"
        "/recargar 5"
    )

@rt.message(F.text.regexp(r"^/recargar(\s+.+)?"))
async def cmd_recargar(msg: Message):
    parts = (msg.text or "").strip().split()
    if len(parts) != 2:
        await msg.reply("Uso: /recargar <monto>\nEj: /recargar 5")
        return

    try:
        amount = float(parts[1])
        if amount <= 0:
            raise ValueError()
    except Exception:
        await msg.reply("Monto inválido. Ej: /recargar 5")
        return

    pago_id = str(uuid.uuid4())[:8]
    png = build_qr_yape(amount)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Ya pagué", callback_data=f"confirm:{pago_id}")]
    ])

    await msg.reply_photo(
        BufferedInputFile(png, filename="yape.png"),
        caption=f"Escanea este QR para pagar {amount:.2f} {CURRENCY}\nID de pedido: <code>{pago_id}</code>\n\n"
                f"Una vez que pagues, presiona '✅ Ya pagué'.",
        reply_markup=kb
    )

@rt.callback_query(F.data.startswith("confirm:"))
async def confirm_payment(q):
    await q.answer()
    pago_id = q.data.split(":", 1)[1]
    # Aquí no hay validación automática → confirmación manual
    await q.message.reply_text(
        f"🔔 Recibimos tu confirmación del pago ID <code>{pago_id}</code>.\n"
        f"El admin verificará tu Yape y acreditará tus créditos."
    )

# ========= FLASK WEB =========
@app.get("/health")
def health():
    return "ok", 200

# ========= MAIN =========
if __name__ == "__main__":
    import threading
    from waitress import serve

    port = int(os.getenv("PORT", "8080"))

    # Lanzar el bot en un hilo separado
    def run_bot():
        asyncio.run(dp.start_polling(bot))

    threading.Thread(target=run_bot, daemon=True).start()

    # Lanzar Flask en el hilo principal
    serve(app, host="0.0.0.0", port=port)
