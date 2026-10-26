import asyncio
import json
import os
import re
import string
from datetime import date
from pathlib import Path

from google import genai
from google.genai import errors, types
import yaml
from telegram import Update
from telegram.ext import Application, ApplicationBuilder, ContextTypes, MessageHandler, filters


FORMATS_PATH = Path(__file__).with_name("formats.yaml")
MODEL_NAME = "gemini-2.5-flash"
CATEGORY_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
OPTIONAL_SECTION_PATTERN = re.compile(r"\[([^\[\]]*)\]")


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
    for template in formats.values():
        if template.count("[") != template.count("]"):
            raise RuntimeError("Optional format sections must be balanced and grouped")
        if any(not template_fields(match.group(1)) for match in OPTIONAL_SECTION_PATTERN.finditer(template)):
            raise RuntimeError("Optional format sections must contain placeholders")
    return formats, formats_text


def template_fields(template: str) -> set[str]:
    fields = set()
    for _, field_name, _, _ in string.Formatter().parse(template):
        if field_name is not None:
            fields.add(field_name)
    return fields


def render_template(template: str, values: dict[str, str]) -> str:
    rendered = []
    position = 0
    for match in OPTIONAL_SECTION_PATTERN.finditer(template):
        rendered.append(template[position:match.start()])
        section = match.group(1)
        section_fields = template_fields(section)
        if section_fields and all(values.get(field, "").strip() for field in section_fields):
            rendered.append(section)
        position = match.end()
    rendered.append(template[position:])
    return "".join(rendered).format_map(values)


def format_result(result: object, formats: dict[str, str]) -> tuple[str, str]:
    if not isinstance(result, dict):
        raise ValueError("Gemini returned a JSON value instead of an object")
    category = result.get("category")
    fields = result.get("fields")
    if not isinstance(category, str) or category not in formats:
        raise ValueError("Gemini returned an unknown category")
    if not CATEGORY_PATTERN.fullmatch(category):
        raise ValueError("Gemini returned an unsafe category")
    if not isinstance(fields, list):
        raise ValueError("Gemini did not return note fields")

    template = formats[category]
    placeholders = template_fields(OPTIONAL_SECTION_PATTERN.sub("", template))
    values = {}
    for field in fields:
        if not isinstance(field, dict):
            raise ValueError("Gemini returned an invalid note field")
        name = field.get("name")
        value = field.get("value")
        if not isinstance(name, str) or name in values:
            raise ValueError("Gemini returned duplicate or invalid field names")
        if not isinstance(value, str):
            raise ValueError(f"Gemini returned an invalid value for {name}")
        values[name] = value.strip()

    for placeholder in template_fields(template):
        value = values.get(placeholder)
        if placeholder in placeholders and (not isinstance(value, str) or not value.strip()):
            raise ValueError(f"Gemini did not provide {placeholder}")
        if not isinstance(value, str):
            continue
        value = value.strip()
        if "\n" in value or "\r" in value:
            raise ValueError(f"Gemini returned multiline text for {placeholder}")
        values[placeholder] = value

    try:
        formatted_line = render_template(template, values)
    except (KeyError, ValueError) as exc:
        raise ValueError("The configured category format is invalid") from exc
    return category, formatted_line


def append_line(vault_path: Path, category: str, formatted_line: str) -> None:
    vault_path.mkdir(parents=True, exist_ok=True)
    with (vault_path / f"{category}.md").open("a", encoding="utf-8") as markdown_file:
        markdown_file.write(formatted_line + "\n")


def create_client() -> genai.Client:
    return genai.Client(api_key=os.environ["GEMINI_API_KEY"])


def generate_content(client: genai.Client, prompt: str):
    return client.models.generate_content(
        model=MODEL_NAME,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema={
                "type": "object",
                "properties": {
                    "category": {"type": "string"},
                    "fields": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "value": {"type": "string"},
                            },
                            "required": ["name", "value"],
                        },
                    },
                },
                "required": ["category", "fields"],
            },
        ),
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
    client = context.application.bot_data["client"]
    vault_path = context.application.bot_data["vault_path"]
    prompt = (
        "Classify the note and extract values for the selected category. "
        "Return only the requested JSON object. The object must have exactly "
        '"category" and "fields" keys. "fields" must be an array of objects '
        'with "name" and "value" keys, containing the placeholder values needed '
        "by that category. Use today's date for YYYY-MM-DD.\n\n"
        f"formats.yaml:\n{formats_text}\n\n"
        f"Today's date: {date.today().isoformat()}\n\n"
        f"Note:\n{message.text}"
    )

    try:
        response = await asyncio.to_thread(generate_content, client, prompt)
        result = json.loads(response.text)
        category, formatted_line = format_result(result, formats)
        await asyncio.to_thread(append_line, vault_path, category, formatted_line)
    except (json.JSONDecodeError, ValueError, OSError, RuntimeError) as exc:
        await message.reply_text(f"Could not save your note: {exc}")
        return
    except errors.APIError as exc:
        await message.reply_text(f"Could not save your note because the formatting service failed: {exc}")
        return

    await message.reply_text(f"Saved to {category}.md")


async def post_init(application: Application) -> None:
    try:
        formats, formats_text = load_formats()
        application.bot_data["formats"] = formats
        application.bot_data["formats_text"] = formats_text
        application.bot_data["client"] = create_client()
        application.bot_data["allowed_user_id"] = int(os.environ["ALLOWED_USER_ID"])
        application.bot_data["vault_path"] = Path(os.environ.get("VAULT_PATH", "vault")).expanduser()
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
