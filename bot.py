import asyncio
import logging
import os
import tempfile

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from anthropic import AsyncAnthropic
from openai import AsyncOpenAI

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.environ["BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]  # тільки для розшифровки голосових
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5")
TRANSCRIBE_MODEL = os.getenv("TRANSCRIBE_MODEL", "gpt-4o-transcribe")
ALLOWED_IDS = {int(x) for x in os.getenv("ALLOWED_IDS", "").split(",") if x.strip()}
PREVIEW_LEN = 1000

claude = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
openai = AsyncOpenAI(api_key=OPENAI_API_KEY)
bot = Bot(BOT_TOKEN)
dp = Dispatcher()

# ---------- формати ----------

BASE_RULES = """Ти перетворюєш розшифровку голосового або чернетку на готовий текст.
Пиши українською. Зберігай факти, імена, цифри, дати з вихідного тексту, нічого не вигадуй.
Прибирай слова-паразити, повтори, самопоправки.
Без штампів, пафосу, канцеляризмів і «AI-тону». Без емодзі. Без вступів на кшталт «Ось ваш текст».
Віддай тільки готовий результат."""

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
    media = message.voice or message.audio or message.video_note or message.video
    ext = ".ogg" if message.voice else ".mp4" if (message.video_note or message.video) else ".mp3"
    tg_file = await bot.get_file(media.file_id)
    with tempfile.NamedTemporaryFile(suffix=ext) as tmp:
        await bot.download_file(tg_file.file_path, destination=tmp.name)
        with open(tmp.name, "rb") as f:
            res = await openai.audio.transcriptions.create(
                model=TRANSCRIBE_MODEL, file=f, language="uk"
            )
    return res.text.strip()


async def run_format(source: str, fmt: str, extra: str = "") -> str:
    system = f"{BASE_RULES}\n\n{FORMATS[fmt]['prompt']}"
    content = f"Вихідний текст:\n{source}"
    if extra:
        content += f"\n\nВідповідь на уточнювальні питання:\n{extra}"
    resp = await claude.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=2000,
        system=system,
        messages=[{"role": "user", "content": content}],
    )
    return resp.content[0].text.strip()


async def deliver(message: Message, uid: int, fmt: str, extra: str = ""):
    state = st(uid)
    await bot.send_chat_action(message.chat.id, "typing")
    result = await run_format(state["text"], fmt, extra)
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
        return
    await bot.send_chat_action(message.chat.id, "typing")
    try:
        text = await transcribe(message)
    except Exception:
        logging.exception("transcribe failed")
        await message.answer("Не вдалося розпізнати. Спробуй ще раз або кинь текстом.")
        return
    if not text:
        await message.answer("Розшифровка порожня. Перевір, чи є звук у записі.")
        return
    await handle_input(message, text)


@dp.message(F.text & ~F.text.startswith("/"))
async def on_text(message: Message):
    if not allowed(message.from_user.id):
        return
    await handle_input(message, message.text.strip())


@dp.callback_query(F.data.startswith("f:"))
async def on_format(cb: CallbackQuery):
    uid = cb.from_user.id
    if not allowed(uid):
        return
    state = st(uid)
    if not state["text"]:
        await cb.answer("Спочатку кинь голосове або текст", show_alert=True)
        return
    await cb.answer()
    await deliver(cb.message, uid, cb.data.split(":", 1)[1])


@dp.callback_query(F.data == "add")
async def on_add(cb: CallbackQuery):
    st(cb.from_user.id)["adding"] = True
    await cb.answer()
    await cb.message.answer("Кидай ще голосове або текст, додам до попереднього.")


@dp.callback_query(F.data == "full")
async def on_full(cb: CallbackQuery):
    text = st(cb.from_user.id)["text"]
    await cb.answer()
    if len(text) <= 4000:
        await cb.message.answer(text)
    else:
        await cb.message.answer_document(
            BufferedInputFile(text.encode("utf-8"), filename="розшифровка.txt")
        )


async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
