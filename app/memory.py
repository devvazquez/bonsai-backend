"""Per-device persistent memory, in SQLite.

SQLite instead of an external service (Redis, etc.) to avoid depending on
anything else: the file lives in the container's persistent volume.
"""

from __future__ import annotations

import os
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any


# Order matters: BONSAI_DB_PATH wins, then /data (the VPS volume), then ./data
# next to the code (local runs, any OS).
def _default_db_path() -> str:
    if os.path.isdir("/data"):
        return "/data/bonsai.db"
    local_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
    os.makedirs(local_dir, exist_ok=True)
    return os.path.join(local_dir, "bonsai.db")


DB_PATH = os.environ.get("BONSAI_DB_PATH") or _default_db_path()

MAX_ITEMS = 50  # per-device cap, so the prompt does not blow up

# Photos live next to the database, not inside it: a BLOB column per photo
# would grow the file until any SELECT got slow, and the table only needs the path.
CAPTURES_DIR = os.environ.get("BONSAI_CAPTURES_DIR") or os.path.join(
    os.path.dirname(os.path.abspath(DB_PATH)), "captures"
)

# Conversations kept per device. Beyond this the oldest rows and their files are
# deleted, otherwise the disk grows forever.
MAX_CAPTURES = int(os.environ.get("BONSAI_MAX_CAPTURES", "100"))


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS memories (
                id         TEXT PRIMARY KEY,
                device_id  TEXT NOT NULL,
                fact       TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_device ON memories(device_id)"
        )
        # What happened in each /ask: photo, what was said and what was answered.
        # Lets you debug without reproducing the scene; shows up in /admin.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS captures (
                id          TEXT PRIMARY KEY,
                device_id   TEXT NOT NULL,
                created_at  TEXT NOT NULL,
                image_path  TEXT NOT NULL,
                image_bytes INTEGER NOT NULL,
                audio_secs  REAL,
                transcript  TEXT,
                reply       TEXT,
                stt_ms      INTEGER,
                vision_ms   INTEGER,
                total_ms    INTEGER
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_captures_device "
            "ON captures(device_id, created_at)"
        )


def list_memories(device_id: str) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, fact, created_at FROM memories "
            "WHERE device_id = ? ORDER BY created_at",
            (device_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def add_memory(device_id: str, fact: str) -> dict[str, Any]:
    item = {
        "id": str(uuid.uuid4()),
        "fact": fact,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    with _connect() as conn:
        conn.execute(
            "INSERT INTO memories (id, device_id, fact, created_at) "
            "VALUES (?, ?, ?, ?)",
            (item["id"], device_id, item["fact"], item["created_at"]),
        )
        # Keep only the MAX_ITEMS most recent ones for this device.
        conn.execute(
            """
            DELETE FROM memories
            WHERE device_id = ? AND id NOT IN (
                SELECT id FROM memories
                WHERE device_id = ?
                ORDER BY created_at DESC
                LIMIT ?
            )
            """,
            (device_id, device_id, MAX_ITEMS),
        )
    return item


def delete_memory(device_id: str, memory_id: str) -> bool:
    with _connect() as conn:
        # LIKE so an id prefix is enough, for convenience.
        cur = conn.execute(
            "DELETE FROM memories WHERE device_id = ? AND id LIKE ?",
            (device_id, f"{memory_id}%"),
        )
    return cur.rowcount > 0


def update_memory(device_id: str, memory_id: str, fact: str) -> dict[str, Any] | None:
    """Fixes the text of a memory. Returns None if it does not exist."""
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE memories SET fact = ? WHERE device_id = ? AND id LIKE ?",
            (fact, device_id, f"{memory_id}%"),
        )
        if cur.rowcount == 0:
            return None
        row = conn.execute(
            "SELECT id, fact, created_at FROM memories "
            "WHERE device_id = ? AND id LIKE ?",
            (device_id, f"{memory_id}%"),
        ).fetchone()
    return dict(row) if row else None


def list_devices() -> list[dict[str, Any]]:
    """Every device that has memories, with how many and from when.

    Needed to browse the database without knowing the deviceIds by heart.
    """
    with _connect() as conn:
        rows = conn.execute(
            "SELECT device_id, COUNT(*) AS total, "
            "       MIN(created_at) AS first_at, MAX(created_at) AS last_at "
            "FROM memories GROUP BY device_id ORDER BY last_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def stats() -> dict[str, Any]:
    with _connect() as conn:
        total = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        devices = conn.execute(
            "SELECT COUNT(DISTINCT device_id) FROM memories"
        ).fetchone()[0]
    try:
        size = os.path.getsize(DB_PATH)
    except OSError:
        size = 0
    return {
        "memories": total,
        "devices": devices,
        "dbPath": DB_PATH,
        "dbBytes": size,
        "maxPerDevice": MAX_ITEMS,
    }


def clear_device(device_id: str) -> int:
    """Deletes every memory of a device. Returns how many there were."""
    with _connect() as conn:
        cur = conn.execute("DELETE FROM memories WHERE device_id = ?", (device_id,))
    return cur.rowcount


# --------------------------------------------------------------------------
# Photos and conversations from /ask
# --------------------------------------------------------------------------
def _safe_name(device_id: str) -> str:
    """A deviceId arrives over the network and ends up as a folder name.

    Without this, a deviceId like "../../etc" would write outside the directory.
    """
    clean = "".join(c if c.isalnum() or c in "-_" else "-" for c in device_id)
    return clean[:64] or "desconegut"


def save_capture(device_id: str, image: bytes) -> tuple[str, str]:
    """Stores the photo on disk and returns (id, path).

    Written as soon as it arrives, before transcribing or describing anything:
    if something fails later, the photo is still there to look at.
    """
    capture_id = str(uuid.uuid4())
    folder = os.path.join(CAPTURES_DIR, _safe_name(device_id))
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"{capture_id}.jpg")
    with open(path, "wb") as f:
        f.write(image)

    with _connect() as conn:
        conn.execute(
            "INSERT INTO captures (id, device_id, created_at, image_path, "
            "image_bytes) VALUES (?, ?, ?, ?, ?)",
            (capture_id, device_id, datetime.now(timezone.utc).isoformat(),
             path, len(image)),
        )
    return capture_id, path


def finish_capture(capture_id: str, **fields: Any) -> None:
    """Fills in the row once we know what was said and what was answered.

    Column names come from this allowlist and not from the caller: they go
    inside the SQL because a column cannot be a bound parameter.
    """
    allowed = ("audio_secs", "transcript", "reply", "stt_ms", "vision_ms",
               "total_ms")
    pairs = [(k, v) for k, v in fields.items() if k in allowed]
    if not pairs:
        return
    sets = ", ".join(f"{k} = ?" for k, _ in pairs)
    with _connect() as conn:
        conn.execute(
            f"UPDATE captures SET {sets} WHERE id = ?",
            [v for _, v in pairs] + [capture_id],
        )


def prune_captures(device_id: str) -> int:
    """Keeps only the last MAX_CAPTURES of this device, files included."""
    with _connect() as conn:
        old = conn.execute(
            "SELECT id, image_path FROM captures WHERE device_id = ? "
            "ORDER BY created_at DESC LIMIT -1 OFFSET ?",
            (device_id, MAX_CAPTURES),
        ).fetchall()
        for row in old:
            try:
                os.remove(row["image_path"])
            except OSError:
                # File already gone or undeletable: the row goes away anyway.
                pass
            conn.execute("DELETE FROM captures WHERE id = ?", (row["id"],))
    return len(old)


def get_capture(capture_id: str) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM captures WHERE id LIKE ?", (f"{capture_id}%",)
        ).fetchone()
    return dict(row) if row else None


def list_captures(device_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    sql = ("SELECT id, device_id, created_at, image_bytes, audio_secs, "
           "transcript, reply, stt_ms, vision_ms, total_ms FROM captures")
    params: list[Any] = []
    if device_id:
        sql += " WHERE device_id = ?"
        params.append(device_id)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    with _connect() as conn:
        return [dict(r) for r in conn.execute(sql, params)]


def recent_history(device_id: str, max_turns: int, max_minutes: int) -> list[tuple[str, str]]:
    """Last conversation turns for this device, as (user text, assistant text).

    Text only, oldest first — the photo is never replayed. Groq charges tokens
    per image, and a shot from a while ago is not "context" anymore: the person
    doesn't have it in front of them. Windowed by time too, so a question from
    an hour ago doesn't leak into "just now".
    """
    if max_turns <= 0 or max_minutes <= 0:
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=max_minutes)).isoformat()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT transcript, reply FROM captures "
            "WHERE device_id = ? AND created_at >= ? "
            "AND transcript IS NOT NULL AND transcript != '' "
            "AND reply IS NOT NULL AND reply != '' "
            "ORDER BY created_at DESC LIMIT ?",
            (device_id, cutoff, max_turns),
        ).fetchall()
    return [(r["transcript"], r["reply"]) for r in reversed(rows)]


def get_memory_context(device_id: str) -> str:
    """Returns the memories as plain text, to inject into the system prompt."""
    items = list_memories(device_id)
    if not items:
        return ""
    return "\n".join(f"- {i['fact']}" for i in items)
