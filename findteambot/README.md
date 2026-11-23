# Бот «Союзники»

1. Создай бота в @BotFather → получишь токен.
2. Нужен публичный HTTPS-адрес (Telegram открывает Mini App только по https):
   - для теста: `cloudflared tunnel --url http://localhost:8080` (или ngrok) → скопируй https-ссылку;
   - для постоянной работы: VPS / Railway / Render с этим же кодом.
3. Запуск:
   ```
   pip install -r requirements.txt
   export BOT_TOKEN="токен_от_BotFather"
   export WEBAPP_URL="https://твой-адрес"
   python bot.py
   ```
4. Открой бота в Telegram → /start → «Открыть».

Данные хранятся в файле allies.db рядом с bot.py (на хостинге с временным диском подключи постоянный том).
