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


def _chart_resend_followup(message, history):
    text = unicodedata.normalize("NFKC", str(message or "")).strip()
    if not re.fullmatch(r"(?:你|您|请|麻烦|麻烦你|可以|能不能|帮我|给我|那)*(?:再|重新)(?:给我|帮我|给|帮)?(?:发|发送)(?:一下|一次|一遍|一张|下)?(?:给我)?(?:可以吗|好吗|吗|哈|吧|呀|呢|嘛)?[?？!！。，,\s]*", text):
        return False
    turns = [entry for entry in (history or []) if isinstance(entry, dict)
             and entry.get("role") in {"user", "assistant"}]
    last = turns[-1] if turns else None
    if not last or last.get("role") != "assistant":
        return False
    chart = r"尺码(?:表|图|对照表|对照图)|码数表|对照表"
    last_text = str(last.get("content") or last.get("text") or "").strip()
    if re.search(chart, last_text):
        return True
    return bool(last_text == "我看看哈" and len(turns) >= 2 and turns[-2].get("role") == "user"
                and re.search(chart, str(turns[-2].get("content") or turns[-2].get("text") or "")))


def wants_chart(message, history=None):
    text = unicodedata.normalize("NFKC", str(message or ""))
    return bool(re.search(r"脚长|脚宽|足长|足宽|尺码|鞋码|码数|几码|多少码|多大码|选码|内长|合脚|偏码|偏大|偏小|尺码表|尺码图|码数表|对照表|(?:3[0-9]|4[0-9]|50)(?:\.5)?\s*码|(?:运动鞋|皮鞋)(?:平时)?穿\s*(?:3[0-9]|4[0-9]|50)(?:\.5)?", text) or _chart_resend_followup(text, history))


def explicitly_requests_chart(message, history=None):
    text = unicodedata.normalize("NFKC", str(message or ""))
    return bool(re.search(r"(?:发|给|看|提供|来|再发).{0,12}(?:尺码表|尺码图|尺码对照图|码数表|对照表)|(?:尺码表|尺码图|尺码对照图|码数表|对照表).{0,12}(?:发|给|看|有吗|在哪)", text) or _chart_resend_followup(text, history))


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
