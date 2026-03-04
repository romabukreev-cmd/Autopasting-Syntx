import os
from dotenv import load_dotenv

load_dotenv()

# Telegram
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0"))

# OpenRouter
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

# Models
MODEL_ANALYZER = "openai/gpt-4o"
MODEL_IMAGE_1 = "bytedance-seed/seedream-4.5"
MODEL_IMAGE_2 = "google/gemini-3.1-flash-image-preview"

# Google Sheets (публичная ссылка — Sheet должен быть "Все со ссылкой могут просматривать")
# ID из URL таблицы: docs.google.com/spreadsheets/d/{GSHEETS_ID}/...
GSHEETS_ID = os.getenv("GSHEETS_ID")

# Make.com
MAKE_WEBHOOK_URL = os.getenv("MAKE_WEBHOOK_URL")
MAKE_PIN_LINK = os.getenv("MAKE_PIN_LINK")  # ссылка на Telegram-канал

# Database
DB_PATH = "syntx.db"

# Google Drive (через rclone, remote = gdrive:)
DRIVE_BASE_PATH = os.getenv("DRIVE_BASE_PATH", "PROJECTS/Автопостинг Syntx")
DRIVE_FOLDER_REFS = "Референс"
DRIVE_FOLDER_GENS = "База генераций"

# Generation
IMAGES_PER_WEEK = 100
GENERATIONS_PER_PROMPT = 5  # сколько изображений генерить на каждой модели с одного промпта
IMAGES_PER_DAY_MIN = 15
IMAGES_PER_DAY_MAX = 20

# Delays (seconds)
DELAY_BETWEEN_GENERATIONS = 2
DELAY_GDRIVE_DOWNLOAD = 0.5
DELAY_MAKE_WEBHOOK = 3

# Retry
MAX_GENERATION_ATTEMPTS = 3
RETRY_DELAY = 30

# Cleanup
PINTEREST_FILE_TTL_DAYS = 30

# Timezone
TIMEZONE = "Europe/Moscow"

# Telegram channel posting
TG_CHANNEL_ID = os.getenv("TG_CHANNEL_ID", "@roman_s_ai")
MODEL_TG_POST = "anthropic/claude-sonnet-4-6"
TG_POST_HOUR_START = 10   # earliest hour for TG post (MSK)
TG_POST_HOUR_END = 22     # latest hour for TG post (MSK)
