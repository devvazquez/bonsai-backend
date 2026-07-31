"""Memoria persistente por dispositivo, en SQLite.

Se usa SQLite en vez de un servicio externo (Redis, etc.) para no depender de
nada más: el fichero vive en el volumen persistente del contenedor.
"""

from __future__ import annotations

import os
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

# Dónde vive la base de datos:
#   1. BONSAI_DB_PATH, si se define (tiene prioridad sobre todo lo demás).
#   2. /data, el volumen persistente del contenedor (así es en la VPS).
#   3. ./data, junto al código (así es al ejecutarlo en local, en cualquier SO).
def _default_db_path() -> str:
    if os.path.isdir("/data"):
        return "/data/bonsai.db"
    local_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
    os.makedirs(local_dir, exist_ok=True)
    return os.path.join(local_dir, "bonsai.db")


DB_PATH = os.environ.get("BONSAI_DB_PATH") or _default_db_path()

MAX_ITEMS = 50  # límite por dispositivo, para no saturar el prompt


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
        # Conserva solo los MAX_ITEMS más recientes de este dispositivo.
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
        # Permite pasar solo el prefijo del id, por comodidad.
        cur = conn.execute(
            "DELETE FROM memories WHERE device_id = ? AND id LIKE ?",
            (device_id, f"{memory_id}%"),
        )
    return cur.rowcount > 0


def get_memory_context(device_id: str) -> str:
    """Devuelve los recuerdos en texto plano para inyectar en el system prompt."""
    items = list_memories(device_id)
    if not items:
        return ""
    return "\n".join(f"- {i['fact']}" for i in items)
