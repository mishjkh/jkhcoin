import hashlib
import hmac
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator
from urllib.parse import urlsplit

from fastapi import FastAPI, Header, HTTPException, Request
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
    WebAppInfo,
)
from telegram.ext import Application, CommandHandler, ContextTypes


WEB_APP_URL = "https://mishjkh.github.io/jkhcoin/"
WELCOME_IMAGE_URL = (
    "https://mishjkh.github.io/jkhcoin/photo_2026-10-28_18-50-34.jpg"
)

telegram_application: Application | None = None


def get_webhook_secret() -> str:
    secret = os.environ.get("WEBHOOK_SECRET")
    if not secret:
        raise RuntimeError("WEBHOOK_SECRET must be configured as a hosting secret.")
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Открыть ЖКХ Коин",
                    web_app=WebAppInfo(url=WEB_APP_URL),
                )
            ]
        ]
    )
    await message.reply_photo(
        photo=WELCOME_IMAGE_URL,
        caption="Добро пожаловать в мир ЖКХ!",
        reply_markup=keyboard,
    )


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    global telegram_application

    token = os.environ.get("BOT_TOKEN")
    public_url = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
    parsed_url = urlsplit(public_url)

    if not token:
        raise RuntimeError("BOT_TOKEN must be configured as a hosting secret.")
    webhook_secret = get_webhook_secret()
    if parsed_url.scheme != "https" or not parsed_url.netloc:
        raise RuntimeError("RENDER_EXTERNAL_URL must be the public HTTPS service URL.")

    telegram_application = Application.builder().token(token).build()
    telegram_application.add_handler(CommandHandler("start", start))
    await telegram_application.initialize()
    await telegram_application.start()
    await telegram_application.bot.set_webhook(
        url=f"{public_url}/telegram",
        secret_token=webhook_secret,
        allowed_updates=Update.ALL_TYPES,
    )

    try:
        yield
    finally:
        await telegram_application.stop()
        await telegram_application.shutdown()
        telegram_application = None


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def health_check() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/telegram")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict[str, bool]:
    if not hmac.compare_digest(
        x_telegram_bot_api_secret_token or "",
        get_webhook_secret(),
    ):
        raise HTTPException(status_code=403, detail="Invalid webhook secret")
    if telegram_application is None:
        raise HTTPException(status_code=503, detail="Bot is starting")

    update_data = await request.json()
    update = Update.de_json(update_data, telegram_application.bot)
    await telegram_application.process_update(update)
    return {"ok": True}
