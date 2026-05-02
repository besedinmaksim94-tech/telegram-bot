import asyncio
import logging
import os

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import Message
from openai import AsyncOpenAI

# ================= НАСТРОЙКИ =================

# 👉 ВСТАВЬ СВОИ ДАННЫЕ (или через Railway Variables)
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")  # токен бота
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")  # ключ OpenAI

# Ограничение длины сообщения
MAX_MESSAGE_LENGTH = 2000

# ================= ИНИЦИАЛИЗАЦИЯ =================

logging.basicConfig(level=logging.INFO)

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()

client = AsyncOpenAI(api_key=OPENAI_API_KEY)

# ================= КОМАНДЫ =================

@dp.message(Command("start"))
async def start_handler(message: Message):
    await message.answer(
        "👋 <b>Привет!</b>\n\n"
        "🤖 Я бот, который решает задачи с помощью AI\n\n"
        "✏️ Просто отправь мне задачу текстом — я отвечу 😉",
        parse_mode="HTML"
    )

# ================= ОСНОВНАЯ ЛОГИКА =================

@dp.message()
async def handle_message(message: Message):
    user_text = message.text

    # Проверка на пустой текст
    if not user_text:
        return await message.answer("❗ Отправь текстовое сообщение")

    # Ограничение длины
    if len(user_text) > MAX_MESSAGE_LENGTH:
        return await message.answer("❗ Слишком длинное сообщение")

    # Показываем "печатает..."
    await bot.send_chat_action(message.chat.id, "typing")

    try:
        # Запрос к OpenAI
        response = await client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[
                {"role": "system", "content": "Ты полезный помощник."},
                {"role": "user", "content": user_text}
            ],
            temperature=0.7,
        )

        answer = response.choices[0].message.content

        # Ограничение ответа Telegram
        if len(answer) > 4096:
            answer = answer[:4096]

        await message.answer(answer)

    except Exception as e:
        logging.error(f"Ошибка: {e}")

        await message.answer(
            "⚠️ Ошибка при обращении к AI\n"
            "Попробуй позже"
        )

# ================= ЗАПУСК =================

async def main():
    if not TELEGRAM_TOKEN or not OPENAI_API_KEY:
        raise ValueError("❌ Укажи TELEGRAM_TOKEN и OPENAI_API_KEY")

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())