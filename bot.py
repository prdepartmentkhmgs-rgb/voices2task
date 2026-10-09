import asyncio
import io
import logging
import os

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from google import genai
from google.genai import types

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.environ["BOT_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
ALLOWED_IDS = {int(x) for x in os.getenv("ALLOWED_IDS", "").split(",") if x.strip()}
PREVIEW_LEN = 1000

client = genai.Client(api_key=GEMINI_API_KEY)
bot = Bot(BOT_TOKEN)
dp = Dispatcher()

# ---------- формати ----------

BASE_RULES = """Ти перетворюєш розшифровку голосового або чернетку на готовий текст.
Пиши українською. Зберігай факти, імена, цифри, дати з вихідного тексту, нічого не вигадуй.
Прибирай слова-паразити, повтори, самопоправки.
Без штампів, пафосу, канцеляризмів і «AI-тону». Без емодзі. Без вступів на кшталт «Ось ваш текст».
Віддай тільки готовий результат."""

TRANSCRIBE_PROMPT = (
    "Розшифруй це аудіо дослівно, мовою оригіналу (зазвичай українська). "
    "Став розділові знаки. Не додавай жодних коментарів, заголовків чи пояснень, "
    "поверни тільки текст розшифровки."
)

FORMATS = {
    "msg": {
        "label": "💬 Повідомлення",
        "prompt": "Формат: коротке повідомлення в робочий чат. 1–4 речення, по суті, без привітань на пів абзацу. Тон живий, як між колегами.",
    },
    "task": {
        "label": "✅ Задача",
        "prompt": """Формат: задача для трекера. Структура:
Назва: (дієслово + результат, до 8 слів)
Виконавець:
Дедлайн:
Критерій готовності:
Опис: (1–3 речення контексту)

Якщо у вихідному тексті немає виконавця, дедлайну або критерію готовності, НЕ вигадуй їх. Тоді віддай лише блок у такому вигляді (і більше нічого):
БРАКУЄ:
1. ВИКОНАВЕЦЬ — ...?
2. ДЕДЛАЙН — ...?
(тільки ті пункти, яких справді бракує; питання конкретні, під цю задачу)""",
    },
    "followup": {
        "label": "📩 Фолоуап",
        "prompt": "Формат: фолоуап після розмови чи зустрічі. Спочатку одним реченням що обговорили, далі що домовились (хто, що, коли), у кінці наступний крок. Короткі абзаци.",
    },
    "copy": {
        "label": "✍️ Копірайтер",
        "prompt": "Формат: текст допису для Instagram. 2–3 короткі абзаци. Перше речення чіпляє без кліше та питань до аудиторії. Без закликів «долучайся», без вигуків, без хештегів, якщо їх не просили.",
    },
    "ceo": {
        "label": "📊 Звіт CEO",
        "prompt": "Формат: короткий звіт для керівника. Блоки: Зроблено / В роботі / Ризики і блокери / Потрібне рішення. Порожні блоки пропускай. Цифри й терміни зберігай точно. Не більше 10 рядків.",
    },
}

# слово на початку повідомлення вмикає формат без кнопок
KEYWORDS = {
    "повідомлення": "msg",
    "коментар": "msg",
    "статус": "msg",
    "задача": "task",
    "фолоуап": "followup",
    "копірайтер": "copy",
    "пост": "copy",
    "звіт": "ceo",
}

# user_id -> {"text": str, "adding": bool, "pending": {"fmt": str} | None}
STATE: dict[int, dict] = {}


def st(uid: int) -> dict:
    return STATE.setdefault(uid, {"text": "", "adding": False, "pending": None})


def allowed(uid: int) -> bool:
    return not ALLOWED_IDS or uid in ALLOWED_IDS


async def deny(message: Message):
    await message.answer(f"Бот приватний. Твій Telegram ID: {message.from_user.id}")


def err_text(e: Exception) -> str:
    s = str(e)
    if "429" in s or "RESOURCE_EXHAUSTED" in s:
        return "Ліміт Gemini на зараз вичерпано. Спробуй через хвилину."
    if "API key" in s or "403" in s or "401" in s:
        return "Gemini не прийняв ключ. Перевір GEMINI_API_KEY у Railway."
    if "404" in s or "NOT_FOUND" in s:
        return f"Модель {MODEL} недоступна. Задай іншу в змінній GEMINI_MODEL."
    return "Щось пішло не так. Спробуй ще раз."


# ---------- допоміжне ----------

async def send_long(message: Message, text: str, **kw):
    for i in range(0, len(text), 4000):
        await message.answer(text[i : i + 4000], **kw)


def menu_kb() -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text=FORMATS["msg"]["label"], callback_data="f:msg"),
            InlineKeyboardButton(text=FORMATS["task"]["label"], callback_data="f:task"),
        ],
        [
            InlineKeyboardButton(text=FORMATS["followup"]["label"], callback_data="f:followup"),
            InlineKeyboardButton(text=FORMATS["copy"]["label"], callback_data="f:copy"),
        ],
        [InlineKeyboardButton(text=FORMATS["ceo"]["label"], callback_data="f:ceo")],
        [
            InlineKeyboardButton(text="➕ Додати ще", callback_data="add"),
            InlineKeyboardButton(text="📄 Повна розшифровка", callback_data="full"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def show_menu(message: Message, text: str):
    shown = min(len(text), PREVIEW_LEN)
    head = f"ℹ️ Показано {shown} із {len(text)} символів. Це лише прев'ю, у роботу піде весь текст."
    body = f"{head}\n\n{text[:PREVIEW_LEN]}\n\nОбери формат:"
    await message.answer(body, reply_markup=menu_kb())


async def transcribe(message: Message) -> str:
    if message.voice:
        media, mime = message.voice, "audio/ogg"
    elif message.audio:
        media, mime = message.audio, message.audio.mime_type or "audio/mpeg"
    elif message.video_note:
        media, mime = message.video_note, "video/mp4"
    else:
        media, mime = message.video, "video/mp4"
    buf = io.BytesIO()
    await bot.download(media, destination=buf)
    resp = await client.aio.models.generate_content(
        model=MODEL,
        contents=[
            types.Part.from_bytes(data=buf.getvalue(), mime_type=mime),
            TRANSCRIBE_PROMPT,
        ],
    )
    return (resp.text or "").strip()


async def run_format(source: str, fmt: str, extra: str = "") -> str:
    system = f"{BASE_RULES}\n\n{FORMATS[fmt]['prompt']}"
    content = f"Вихідний текст:\n{source}"
    if extra:
        content += f"\n\nВідповідь на уточнювальні питання:\n{extra}"
    resp = await client.aio.models.generate_content(
        model=MODEL,
        contents=content,
        config=types.GenerateContentConfig(system_instruction=system, temperature=0.7),
    )
    return (resp.text or "").strip()


async def deliver(message: Message, uid: int, fmt: str, extra: str = ""):
    state = st(uid)
    await bot.send_chat_action(message.chat.id, "typing")
    try:
        result = await run_format(state["text"], fmt, extra)
    except Exception as e:
        logging.exception("format failed")
        await message.answer(err_text(e))
        return
    if not result:
        await message.answer("Модель повернула порожню відповідь. Спробуй ще раз.")
        return
    if fmt == "task" and result.startswith("БРАКУЄ"):
        state["pending"] = {"fmt": fmt}
        await send_long(message, result)
        return
    state["pending"] = None
    await send_long(message, result)


# ---------- хендлери ----------

@dp.message(CommandStart())
async def start(message: Message):
    if not allowed(message.from_user.id):
        await deny(message)
        return
    await message.answer(
        "Кидай голосове або текст, розшифрую й оформлю.\n\n"
        "Формати: повідомлення, задача, фолоуап, копірайтер, звіт. "
        "Якщо почати з такого слова («задача: ...»), кнопки пропущу й одразу видам результат."
    )


async def handle_input(message: Message, text: str):
    uid = message.from_user.id
    state = st(uid)

    if state["pending"]:
        fmt = state["pending"]["fmt"]
        await deliver(message, uid, fmt, extra=text)
        return

    if state["adding"]:
        state["text"] = f"{state['text']}\n\n{text}"
        state["adding"] = False
        await show_menu(message, state["text"])
        return

    first, _, rest = text.partition(" ")
    key = first.lower().strip(":,.—-")
    if key in KEYWORDS and rest.strip():
        state["text"] = rest.strip()
        await deliver(message, uid, KEYWORDS[key])
        return

    state["text"] = text
    await show_menu(message, text)


@dp.message(F.voice | F.audio | F.video_note | F.video)
async def on_media(message: Message):
    if not allowed(message.from_user.id):
        await deny(message)
        return
    await bot.send_chat_action(message.chat.id, "typing")
    try:
        text = await transcribe(message)
    except Exception as e:
        logging.exception("transcribe failed")
        await message.answer(err_text(e))
        return
    if not text:
        await message.answer("Розшифровка порожня. Перевір, чи є звук у записі.")
        return
    await handle_input(message, text)


@dp.message(F.text & ~F.text.startswith("/"))
async def on_text(message: Message):
    if not allowed(message.from_user.id):
        await deny(message)
        return
    await handle_input(message, message.text.strip())


@dp.callback_query(F.data.startswith("f:"))
async def on_format(cb: CallbackQuery):
    uid = cb.from_user.id
    if not allowed(uid):
        await cb.answer("Немає доступу", show_alert=True)
        return
    state = st(uid)
    if not state["text"]:
        await cb.answer("Спочатку кинь голосове або текст", show_alert=True)
        return
    await cb.answer()
    await deliver(cb.message, uid, cb.data.split(":", 1)[1])


@dp.callback_query(F.data == "add")
async def on_add(cb: CallbackQuery):
    if not allowed(cb.from_user.id):
        await cb.answer("Немає доступу", show_alert=True)
        return
    st(cb.from_user.id)["adding"] = True
    await cb.answer()
    await cb.message.answer("Кидай ще голосове або текст, додам до попереднього.")


@dp.callback_query(F.data == "full")
async def on_full(cb: CallbackQuery):
    if not allowed(cb.from_user.id):
        await cb.answer("Немає доступу", show_alert=True)
        return
    text = st(cb.from_user.id)["text"]
    await cb.answer()
    if not text:
        await cb.message.answer("Поки нема що показувати. Кинь голосове або текст.")
    elif len(text) <= 4000:
        await cb.message.answer(text)
    else:
        await cb.message.answer_document(
            BufferedInputFile(text.encode("utf-8"), filename="розшифровка.txt")
        )


async def main():
    me = await bot.get_me()
    logging.info("Бот @%s запущено, модель %s", me.username, MODEL)
    await bot.delete_webhook(drop_pending_updates=False)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
