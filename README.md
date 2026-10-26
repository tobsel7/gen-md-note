# gen-md-note

Telegram bot that formats text with Google Gemini and appends the result to
category-specific Markdown files.

## Configuration

Set these environment variables:

```sh
export BOT_TOKEN="your-telegram-bot-token"
export GEMINI_API_KEY="your-gemini-api-key"
export ALLOWED_USER_ID="123456789"
export VAULT_PATH="/path/to/your/markdown/vault"
```

Categories and their single-line formats are
defined in `formats.yaml`.

## Run

```sh
python3 -m pip install -r requirements.txt
python3 bot.py
```

To build the Docker image:

```sh
docker build -t gen-md-note .
```

To run the bot:

```sh
docker run -d \
  --name gen-md-note \
  --restart unless-stopped \
  -e BOT_TOKEN="your-telegram-bot-token" \
  -e GEMINI_API_KEY="your-gemini-api-key" \
  -e ALLOWED_USER_ID="123456789" \
  -e VAULT_PATH="/vault" \
  -v "$PWD/vault:/vault" \
  gen-md-note
```
