# Telegram Bot Setup

## 1. Create a bot with @BotFather

Open Telegram, message [@BotFather](https://t.me/BotFather):

```
/newbot
```

Answer the prompts:
- **Bot name:** anything visible, e.g. "Hermes Routing Monitor"
- **Bot username:** must end in `bot`, e.g. `hermes_routing_bot`

BotFather replies with your **token**:
```
Done! Congratulations on your new bot.
...
Use this token to access the HTTP API:
1234567890:AA...SS
```

Copy that token → `TELEGRAM_BOT_TOKEN`.

## 2. Get your chat_id

There are several ways. Simplest:

### Method A — message the bot, then curl

1. Message your bot in Telegram (any text, e.g. "hi")
2. Run:
   ```bash
   curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates"
   ```
3. Look for `"chat":{"id":123456789,...}` in the JSON output.
   That's your `TELEGRAM_CHAT_ID`.

### Method B — use @userinfobot

1. Message [@userinfobot](https://t.me/userinfobot) on Telegram
2. It replies with your user ID — use that as `TELEGRAM_CHAT_ID`.
   (Note: some bots only DM to the chat they were explicitly messaged from,
   so method A is more reliable.)

## 3. Set env vars

```bash
export TELEGRAM_BOT_TOKEN="1234567890:AA...SS"
export TELEGRAM_CHAT_ID="123456789"
```

## 4. Test

```bash
curl -X POST "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/sendMessage" \
  -H "Content-Type: application/json" \
  -d "{\"chat_id\":\"$TELEGRAM_CHAT_ID\",\"text\":\"Test from Hermes proxy\"}"
```

You should receive a "Test from Hermes proxy" message in Telegram.

## 5. (Optional) If you don't want Telegram

Just don't set `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`.
The proxy logs all notifications to stdout instead — no crashes.
