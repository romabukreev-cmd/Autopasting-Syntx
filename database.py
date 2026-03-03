import aiosqlite
from config import DB_PATH


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS refs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filename TEXT NOT NULL,
                category TEXT NOT NULL,
                gdrive_file_id TEXT NOT NULL,
                md5 TEXT NOT NULL,
                processed_at TEXT NOT NULL,
                prompts TEXT NOT NULL DEFAULT '[]'
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS generations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reference_id INTEGER NOT NULL,
                prompt_index INTEGER NOT NULL,
                week_number INTEGER NOT NULL,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending',
                gdrive_file_id TEXT,
                pinterest_file_id TEXT,
                FOREIGN KEY (reference_id) REFERENCES refs(id)
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS pins_schedule (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                generation_id INTEGER NOT NULL,
                gdrive_file_id TEXT NOT NULL,
                category TEXT NOT NULL,
                board_id TEXT NOT NULL,
                scheduled_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                published_at TEXT,
                FOREIGN KEY (generation_id) REFERENCES generations(id)
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS bot_state (
                id INTEGER PRIMARY KEY DEFAULT 1,
                active_week INTEGER NOT NULL DEFAULT 0,
                analysis_status TEXT NOT NULL DEFAULT 'idle',
                generation_status TEXT NOT NULL DEFAULT 'idle',
                posting_status TEXT NOT NULL DEFAULT 'idle',
                posting_start_date TEXT,
                posting_end_date TEXT
            )
        """)

        await db.execute("INSERT OR IGNORE INTO bot_state (id) VALUES (1)")
        await db.commit()


async def get_state() -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM bot_state WHERE id = 1") as cur:
            row = await cur.fetchone()
            return dict(row) if row else {}


async def set_state(**kwargs):
    if not kwargs:
        return
    fields = ", ".join(f"{k} = ?" for k in kwargs)
    values = list(kwargs.values())
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"UPDATE bot_state SET {fields} WHERE id = 1", values)
        await db.commit()
