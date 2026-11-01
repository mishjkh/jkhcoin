import os

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


def main() -> None:
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise RuntimeError(
            "Set BOT_TOKEN in the environment; never put the bot token in source code."
        )

    application = Application.builder().token(token).build()
    application.add_handler(CommandHandler("start", start))
    application.run_polling()


if __name__ == "__main__":
    main()
