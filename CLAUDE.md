# Автопостинг Syntx — паспорт проекта

## Цель
Автоматизация генерации и публикации контента: референсы → AI-генерации → Pinterest.

## Стек
- Python, aiogram 3.x (async Telegram bot)
- APScheduler + SQLite (aiosqlite)
- OpenRouter API (GPT-4o, SeeDream 4.5, Gemini NanaBana)
- Google Drive через rclone (`gdrive:` remote)
- Google Sheets (категории, board_id для Pinterest)
- Make.com (публикация пинов через webhook)
- Pillow (overlay: текст + градиент на изображение)

---

## Архитектура пайплайна

```
Референсы (Drive) → Анализ (GPT-4o) → промпты в DB
                                       ↓
                         Генерация (SeeDream + NanaBana)
                                       ↓
                         Overlay (Pillow: текст + CTA)
                                       ↓
                         Загрузка на Drive (seedream_pin/ + nanobana_pin/)
                                       ↓
                         Расписание (APScheduler → webhook → Make.com → Pinterest)
```

## Модели (config.py)
| Переменная | Модель | Назначение |
|---|---|---|
| `MODEL_ANALYZER` | `openai/gpt-4o` | Анализ референсов, генерация промптов |
| `MODEL_IMAGE_1` | `bytedance-seed/seedream-4.5` | Генерация изображений |
| `MODEL_IMAGE_2` | `google/gemini-3.1-flash-image-preview` | Генерация изображений |

Все вызовы через `https://openrouter.ai/api/v1/chat/completions` с `modalities: ["image", "text"]` и `image_config: {"aspect_ratio": "2:3"}`.

---

## Ключевые файлы

| Файл | Назначение |
|---|---|
| `config.py` | Все константы и модели |
| `database.py` | Создание таблиц + миграции |
| `main.py` | Точка входа, запуск бота и планировщика |
| `modules/bot.py` | Telegram-хэндлеры, клавиатуры |
| `modules/analyzer.py` | GPT-4o анализ референса → 5 вариантов промпта |
| `modules/generator.py` | Генерация через обе модели, overlay, upload |
| `modules/scheduler.py` | Расписание постинга, APScheduler |
| `modules/drive.py` | Обёртка над rclone (upload, download, purge) |
| `modules/overlay.py` | Pillow: тёмный градиент + short prompt + CTA |
| `modules/publisher.py` | Вызов Make.com webhook |
| `modules/sheets.py` | Чтение Google Sheets (категории, board_id) |

---

## База данных (syntx.db)

### `refs`
Референсные изображения. `prompts` — JSON с 5 вариантами `{full, short}`.

### `generations`
Одна запись = один промпт × одна пара изображений (оба файла одного промпта).
- `pinterest_file_id` — Drive ID SeeDream пина (с overlay)
- `nb_pinterest_file_id` — Drive ID NanaBana пина (с overlay)
- `week_number` — к какой неделе постинга относится

### `pins_schedule`
Расписание постинга. Каждая генерация → **2 записи** (SeeDream pin + NanaBana pin).
`gdrive_file_id` → Make.com → Pinterest.

### `bot_state`
Единственная строка: `active_week`, `analysis_status`, `generation_status`, `posting_status`.

---

## Структура Google Drive

```
PROJECTS/Автопостинг Syntx/Pinterest/
├── Референс/
│   └── [категория]/          ← референсные изображения
└── База генераций/
    └── week_{N}/
        └── [категория]/
            ├── seedream/           ← чистая генерация SeeDream
            ├── seedream_pin/       ← с overlay (идут в постинг)
            ├── nanobana/           ← чистая генерация NanaBana
            └── nanobana_pin/       ← с overlay (идут в постинг)
```

---

## Деплой

**Сервер:** `root@85.239.33.163`, сервис: `syntx.service`

```bash
# Деплой
cd ~/Autopasting-Syntx && git pull && sudo systemctl restart syntx && echo OK

# Логи
journalctl -u syntx --since '1 hour ago' --no-pager | tail -100
```

SSH-ключ: `C:/Users/Роман/.ssh/id_ed25519`

---

## Правила работы с кодом

### Можно
- Менять модели через `config.py` — это единственное место
- Добавлять миграции через `try/except` в `database.py:init_db()`
- Переименовывать Drive-папки — они создаются автоматически при генерации

### Нельзя / осторожно
- **Не менять схему промпта в `analyzer.py`** без теста — GPT-4o возвращает JSON, любое изменение структуры ломает парсинг
- **Не трогать `overlay.py`** без визуальной проверки — параметры градиента и шрифта подобраны вручную
- **Не удалять колонки из DB** — только добавлять через ALTER TABLE в init_db
- **Не пушить .env** — там API-ключи
- После смены модели генерации — всегда нажать "🗑 Очистить всё" в боте перед новым запуском

### Перед каждым новым циклом генерации
1. Нажать "🗑 Очистить всё" в боте (очищает DB generations + pins_schedule + Drive gens)
2. Анализ референсов
3. Генерация по неделям (неделя 1, потом 2...)
4. Запустить постинг

---

## Пропорции и форматы
- Все генерации: **2:3** (вертикальный портрет)
- `image_config: {"aspect_ratio": "2:3"}` — передаётся в оба запроса
- Формат файлов на Drive: `.jpg`
- Overlay: short prompt (80-120 символов) + CTA внизу
