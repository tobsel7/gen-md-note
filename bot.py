import asyncio
import json
import os
import re
import string
from pathlib import Path

import google.generativeai as genai
from google.api_core import exceptions as google_api_exceptions
import yaml
from google.generativeai import GenerationConfig
from telegram import Update
from telegram.ext import Application, ApplicationBuilder, ContextTypes, MessageHandler, filters


FORMATS_PATH = Path(__file__).with_name("formats.yaml")
MODEL_NAME = "gemini-1.5-flash"
CATEGORY_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


def load_formats() -> tuple[dict[str, str], str]:
    try:
        formats_text = FORMATS_PATH.read_text(encoding="utf-8")
        formats = yaml.safe_load(formats_text)
    except (OSError, yaml.YAMLError) as exc:
        raise RuntimeError(f"Could not load {FORMATS_PATH}: {exc}") from exc

    if not isinstance(formats, dict) or not formats:
        raise RuntimeError(f"{FORMATS_PATH} must contain a non-empty mapping")
    if any(not isinstance(category, str) or not category for category in formats):
        raise RuntimeError("Format categories must be non-empty strings")
    if any(not isinstance(template, str) or not template for template in formats.values()):
        raise RuntimeError("Format values must be non-empty strings")
    return formats, formats_text


def line_matches_format(line: str, template: str) -> bool:
    pattern = []
    formatter = string.Formatter()
    try:
        fields = list(formatter.parse(template))
    except ValueError:
        return False

    for literal, field_name, format_spec, conversion in fields:
        if literal:
            pattern.append(re.escape(literal))
        if field_name is None:
            continue
        if format_spec or conversion or not field_name.isidentifier():
            return False
        pattern.append(r".+?")
    return bool(re.fullmatch("".join(pattern), line))


def validate_result(result: object, formats: dict[str, str]) -> tuple[str, str]:
    if not isinstance(result, dict):
        raise ValueError("Gemini returned a JSON value instead of an object")
    category = result.get("category")
    formatted_line = result.get("formatted_line")
    if not isinstance(category, str) or category not in formats:
        raise ValueError("Gemini returned an unknown category")
    if not CATEGORY_PATTERN.fullmatch(category):
        raise ValueError("Gemini returned an unsafe category")
    if not isinstance(formatted_line, str) or not formatted_line.strip():
        raise ValueError("Gemini returned an empty formatted line")
    if formatted_line != formatted_line.strip() or "\n" in formatted_line or "\r" in formatted_line:
        raise ValueError("Gemini returned a multiline or padded formatted line")
    if not line_matches_format(formatted_line, formats[category]):
        raise ValueError("Gemini returned a line that does not match the category format")
    return category, formatted_line


def append_line(vault_path: Path, category: str, formatted_line: str) -> None:
    vault_path.mkdir(parents=True, exist_ok=True)
    with (vault_path / f"{category}.md").open("a", encoding="utf-8") as markdown_file:
        markdown_file.write(formatted_line + "\n")


def create_model() -> genai.GenerativeModel:
    genai.configure(api_key=os.environ["GEMINI_API_KEY"])
    return genai.GenerativeModel(
        MODEL_NAME,
        generation_config=GenerationConfig(
            response_mime_type="application/json",
            response_schema={
                "type": "object",
                "properties": {
                    "category": {"type": "string"},
                    "formatted_line": {"type": "string"},
                },
                "required": ["category", "formatted_line"],
            },
        )
    )


async def handle_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    user = update.effective_user
    message = update.effective_message
    if user is None or message is None:
        return

    if user.id != context.application.bot_data["allowed_user_id"]:
        return

    formats = context.application.bot_data["formats"]
    formats_text = context.application.bot_data["formats_text"]
    model = context.application.bot_data["model"]
    vault_path = context.application.bot_data["vault_path"]
    prompt = (
        "Classify and format the following note. Return only the requested JSON object.\n"
        'The object must have exactly "category" and "formatted_line" keys.\n'
        "Choose category from the formats below and follow its format exactly. "
        "The formatted line must be one line.\n\n"
        f"formats.yaml:\n{formats_text}\n\n"
        f"Note:\n{message.text}"
    )

    try:
        response = await asyncio.to_thread(model.generate_content, prompt)
        result = json.loads(response.text)
        category, formatted_line = validate_result(result, formats)
        await asyncio.to_thread(append_line, vault_path, category, formatted_line)
    except (json.JSONDecodeError, ValueError, OSError, RuntimeError) as exc:
        await message.reply_text(f"Could not save your note: {exc}")
        return
    except google_api_exceptions.GoogleAPIError:
        await message.reply_text("Could not save your note because the formatting service failed.")
        return

    await message.reply_text(f"Saved to {category}.md")


async def post_init(application: Application) -> None:
    try:
        formats, formats_text = load_formats()
        application.bot_data["formats"] = formats
        application.bot_data["formats_text"] = formats_text
        application.bot_data["model"] = create_model()
        application.bot_data["allowed_user_id"] = int(os.environ["ALLOWED_USER_ID"])
        application.bot_data["vault_path"] = Path(os.environ.get("VAULT_PATH")).expanduser()
    except (KeyError, ValueError, RuntimeError) as exc:
        raise RuntimeError(f"Invalid bot configuration: {exc}") from exc


def build_application() -> Application:
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN is not set")
    return (
        ApplicationBuilder()
        .token(token)
        .post_init(post_init)
        .build()
    )


def main() -> None:
    application = build_application()
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    application.run_polling()


if __name__ == "__main__":
    main()
