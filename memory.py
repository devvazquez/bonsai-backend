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

# Las fotos de /ask se guardan junto a la base de datos, no dentro: una
# columna BLOB por foto haría crecer el fichero hasta hacer lento cualquier
# SELECT, y en la tabla solo hace falta la ruta.
CAPTURAS_DIR = os.environ.get("BONSAI_CAPTURES_DIR") or os.path.join(
    os.path.dirname(os.path.abspath(DB_PATH)), "captures"
)

# Cuántas conversaciones se conservan por dispositivo. Al pasarse se borran
# las más viejas y también su fichero: si no, el disco crece para siempre.
MAX_CAPTURAS = int(os.environ.get("BONSAI_MAX_CAPTURES", "100"))


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
        # Lo que ha pasado en cada /ask: la foto que se guardó, lo que se dijo
        # y lo que contestaron las gafas. Sirve para depurar sin tener que
        # reproducir la escena, y se ve en /admin como una tabla más.
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


def update_memory(device_id: str, memory_id: str, fact: str) -> dict[str, Any] | None:
    """Corrige el texto de un recuerdo. Devuelve None si no existe."""
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
    """Todos los dispositivos que tienen recuerdos, con cuántos y de cuándo.

    Hace falta para poder mirar la base de datos sin saberte de memoria los
    deviceId: hasta ahora había que adivinarlos.
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
        tamano = os.path.getsize(DB_PATH)
    except OSError:
        tamano = 0
    return {
        "memories": total,
        "devices": devices,
        "dbPath": DB_PATH,
        "dbBytes": tamano,
        "maxPerDevice": MAX_ITEMS,
    }


def clear_device(device_id: str) -> int:
    """Borra todos los recuerdos de un dispositivo. Devuelve cuántos eran."""
    with _connect() as conn:
        cur = conn.execute("DELETE FROM memories WHERE device_id = ?", (device_id,))
    return cur.rowcount


# --------------------------------------------------------------------------
# Fotos y conversaciones de /ask
# --------------------------------------------------------------------------
def _nombre_seguro(device_id: str) -> str:
    """Un deviceId llega por la red y acaba siendo un nombre de carpeta.

    Sin esto, un deviceId como "../../etc" escribiría fuera del directorio.
    """
    limpio = "".join(c if c.isalnum() or c in "-_" else "-" for c in device_id)
    return limpio[:64] or "desconegut"


def save_capture(device_id: str, image: bytes) -> tuple[str, str]:
    """Guarda la foto en disco y devuelve (id, ruta).

    Se guarda en cuanto llega, antes de transcribir ni describir nada: si algo
    falla después, la foto sigue ahí para poder ver qué estaba mirando.
    """
    capture_id = str(uuid.uuid4())
    carpeta = os.path.join(CAPTURAS_DIR, _nombre_seguro(device_id))
    os.makedirs(carpeta, exist_ok=True)
    ruta = os.path.join(carpeta, f"{capture_id}.jpg")
    with open(ruta, "wb") as f:
        f.write(image)

    with _connect() as conn:
        conn.execute(
            "INSERT INTO captures (id, device_id, created_at, image_path, "
            "image_bytes) VALUES (?, ?, ?, ?, ?)",
            (capture_id, device_id, datetime.now(timezone.utc).isoformat(),
             ruta, len(image)),
        )
    return capture_id, ruta


def finish_capture(capture_id: str, **campos: Any) -> None:
    """Rellena la fila cuando ya se sabe qué se dijo y qué se contestó.

    Los nombres de columna son de esta lista y no de lo que llegue: van dentro
    del SQL porque una columna no puede ser un parámetro.
    """
    permitidos = ("audio_secs", "transcript", "reply", "stt_ms", "vision_ms",
                  "total_ms")
    pares = [(k, v) for k, v in campos.items() if k in permitidos]
    if not pares:
        return
    sets = ", ".join(f"{k} = ?" for k, _ in pares)
    with _connect() as conn:
        conn.execute(
            f"UPDATE captures SET {sets} WHERE id = ?",
            [v for _, v in pares] + [capture_id],
        )


def prune_captures(device_id: str) -> int:
    """Deja solo las MAX_CAPTURAS últimas de este dispositivo, ficheros incluidos."""
    with _connect() as conn:
        viejas = conn.execute(
            "SELECT id, image_path FROM captures WHERE device_id = ? "
            "ORDER BY created_at DESC LIMIT -1 OFFSET ?",
            (device_id, MAX_CAPTURAS),
        ).fetchall()
        for fila in viejas:
            try:
                os.remove(fila["image_path"])
            except OSError:
                # El fichero ya no está o no se puede borrar: la fila sí se va.
                pass
            conn.execute("DELETE FROM captures WHERE id = ?", (fila["id"],))
    return len(viejas)


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


def get_memory_context(device_id: str) -> str:
    """Devuelve los recuerdos en texto plano para inyectar en el system prompt."""
    items = list_memories(device_id)
    if not items:
        return ""
    return "\n".join(f"- {i['fact']}" for i in items)
