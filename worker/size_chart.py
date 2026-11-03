"""Account-local size chart selection and durable one-attempt deduplication."""
import hashlib
import os
import re
import sqlite3
import stat
import time
import unicodedata
from pathlib import Path

MAX_BYTES = 4 * 1024 * 1024
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def wants_chart(message):
    text = unicodedata.normalize("NFKC", str(message or ""))
    return bool(re.search(r"脚长|脚宽|足长|足宽|尺码|鞋码|码数|几码|多少码|多大码|选码|内长|合脚|偏码|偏大|偏小|尺码表|码数表|对照表|(?:3[0-9]|4[0-9]|50)(?:\.5)?\s*码|(?:运动鞋|皮鞋)(?:平时)?穿\s*(?:3[0-9]|4[0-9]|50)(?:\.5)?", text))


def explicitly_requests_chart(message):
    text = unicodedata.normalize("NFKC", str(message or ""))
    return bool(re.search(r"(?:发|给|看|提供|来|再发).{0,12}(?:尺码表|码数表|对照表)|(?:尺码表|码数表|对照表).{0,12}(?:发|给|看|有吗|在哪)", text))


def read_chart(state_dir):
    path = Path(state_dir) / "size_chart.png"
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as stream:
            meta = os.fstat(stream.fileno())
            if not stat.S_ISREG(meta.st_mode) or meta.st_size > MAX_BYTES:
                return None
            data = stream.read(MAX_BYTES + 1)
        if not data.startswith(PNG_SIGNATURE) or len(data) > MAX_BYTES:
            return None
        return data, hashlib.sha256(data).hexdigest()
    except OSError:
        return None


def delivery_state(db_path, chat_id, digest):
    """Only ACKed sends prove a buyer has previously received this chart."""
    try:
        with sqlite3.connect(db_path, timeout=10) as conn:
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='size_chart_attempts'").fetchone():
                return "not_sent"
            rows = conn.execute("SELECT status FROM size_chart_attempts WHERE chat_id=? AND digest=?", (str(chat_id), digest)).fetchall()
    except sqlite3.Error:
        return "unknown"
    if any(row[0] == "sent" for row in rows):
        return "sent"
    return "uncertain" if rows else "not_sent"


def reserve(db_path, chat_id, source_id, digest, explicit=False):
    """Reserve before the external send; uncertain attempts never auto-retry."""
    with sqlite3.connect(db_path, timeout=10) as conn:
        conn.execute("PRAGMA busy_timeout = 10000")
        conn.execute("""CREATE TABLE IF NOT EXISTS size_chart_attempts (
            chat_id TEXT NOT NULL, source_id TEXT NOT NULL, digest TEXT NOT NULL,
            status TEXT NOT NULL, created_at REAL NOT NULL,
            PRIMARY KEY(chat_id, source_id))""")
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT 1 FROM size_chart_attempts WHERE chat_id=? AND source_id=?", (str(chat_id), str(source_id))).fetchone():
            return False
        if not explicit and conn.execute("SELECT 1 FROM size_chart_attempts WHERE chat_id=? AND digest=?", (str(chat_id), digest)).fetchone():
            return False
        conn.execute("INSERT INTO size_chart_attempts VALUES (?,?,?,?,?)", (str(chat_id), str(source_id), digest, "attempted", time.time()))
        return True


def finish(db_path, chat_id, source_id, status):
    if status not in {"sent", "failed", "cancelled"}:
        raise ValueError("invalid chart status")
    with sqlite3.connect(db_path, timeout=10) as conn:
        conn.execute("UPDATE size_chart_attempts SET status=? WHERE chat_id=? AND source_id=?", (status, str(chat_id), str(source_id)))
