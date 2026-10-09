# Voice bot (Gemini)

Голосове або текст → розшифровка → готовий текст у обраному форматі.
Один ключ Gemini робить і розшифровку, і оформлення.

## Змінні (Railway → Variables)

```
BOT_TOKEN=токен з @BotFather
GEMINI_API_KEY=ключ з aistudio.google.com
ALLOWED_IDS=твій Telegram ID (число з @userinfobot)
```

Необов'язкова: `GEMINI_MODEL` (за замовчуванням `gemini-3.8-flash`). Якщо модель недоступна, бот напише про це, тоді вкажи іншу, наприклад `gemini-3.5-flash`.

## Запуск

1. Залий `bot.py`, `requirements.txt`, `README.md` у GitHub-репозиторій.
2. Railway: New Project → Deploy from GitHub repo.
3. Variables: додай три змінні вище.
4. Settings → Deploy → Custom Start Command: `python bot.py`.
5. У Deployments → View logs має бути рядок «Бот @... запущено». Напиши боту `/start`.

Якщо бот відповідає «Бот приватний. Твій Telegram ID: ...», у `ALLOWED_IDS` стоїть не той ID. Виправ на показаний.

Локально: `pip install -r requirements.txt`, змінні в оточенні, `python bot.py`.

## Як працює

- Голосове без слова на початку: бот показує прев'ю і кнопки форматів.
- Слово на початку («задача ...», «фолоуап ...», «пост ...», «звіт ...», «статус ...») вмикає формат без кнопок.
- «Додати ще» дописує наступне голосове до поточного тексту.
- У «Задачі» без виконавця, дедлайну чи критерію готовності бот питає, чого бракує, і після відповіді видає готову задачу.

Формати та їхні промпти: словник `FORMATS` у `bot.py`.
