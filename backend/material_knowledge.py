"""Small account-local material catalogue; no platform or model calls.

Imports replace the catalogue in one SQLite transaction while preserving product
bindings. Formula cells use only the XLSX cached value. Exact identifiers always
take precedence over natural-language matching; ambiguous data never picks a row.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import sqlite3
import stat
import unicodedata
import zipfile
from contextlib import contextmanager
from functools import lru_cache
from datetime import datetime, timezone
from pathlib import Path

import openpyxl

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 80 * 1024 * 1024
MAX_ROWS = 10000
MAX_CELL_CHARS = 4000
HEADERS = {"主商品编码": "style_code", "货号": "sku", "颜色": "color",
           "分类": "category", "鞋面": "upper", "内里": "lining",
           "鞋垫": "insole", "鞋底": "sole"}
MATERIAL_FIELDS = ("upper", "lining", "insole", "sole")
REQUIRED_HEADERS = {"货号", "鞋面", "内里", "鞋垫", "鞋底"}
_DASHES = str.maketrans({c: "-" for c in "‐‑‒–—―−﹣－"})
_UNSAFE_KNOWLEDGE_TEXT = re.compile(
    r"https?://|www\.|忽略|提示词|系统提示|指令|执行|管理员|密码|口令|密钥|账号|加微|微信|加群|"
    r"api[\s_-]?key|cookie|authorization|bearer|token|secret|password|prompt|<[^>]+>", re.I)


class MaterialKnowledgeError(ValueError):
    """Invalid workbook, identifier or unsafe local storage."""

    def __init__(self, message: str, code: str = "invalid_material_knowledge"):
        self.message = message
        self.code = code
        super().__init__(message)


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    value = unicodedata.normalize("NFKC", str(value)).translate(_DASHES)
    return re.sub(r"\s+", " ", value).strip()


def normalize_code(value) -> str:
    return re.sub(r"\s+", "", _text(value)).upper()


def knowledge_text_safe(value: str, limit: int = 1500) -> bool:
    """Reject executable markup, instruction text and credential-like content."""
    return (isinstance(value, str) and bool(value.strip()) and len(value) <= limit
            and not any(ord(c) < 32 and c not in "\n\r\t" for c in value)
            and not _UNSAFE_KNOWLEDGE_TEXT.search(unicodedata.normalize("NFKC", value)))


def _identifier(value, label: str) -> str:
    result = _text(value)
    if (not result or len(result) > 200 or any(ord(c) < 32 for c in result)
            or "/" in result or "\\" in result or result in (".", "..")):
        raise MaterialKnowledgeError(f"{label} 无效")
    return result


@lru_cache(maxsize=16384)
def _code_pattern(code):
    return re.compile(r"(?<![A-Z0-9_.-])" + r"\s*".join(re.escape(char) for char in code)
        + r"(?![A-Z0-9_.-]|\s*[-.]\s*[A-Z0-9])")


def _found_codes(text, known):
    normalized = _text(text).upper()
    if not normalized:
        return []
    return [code for code in known if code and _code_pattern(code).search(normalized)]


class MaterialKnowledgeStore:
    def __init__(self, account_dir: Path):
        raw = Path(account_dir).expanduser()
        if ".." in raw.parts:
            raise MaterialKnowledgeError("知识库路径不得包含上级目录")
        self.account_dir = raw.absolute()
        if self.account_dir == Path(self.account_dir.anchor):
            raise MaterialKnowledgeError("知识库必须位于账户目录内")
        self.path = self.account_dir / "material-knowledge.sqlite3"
        self._validate_paths()
        self.account_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.account_dir, 0o700)
        with self._connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS records (
                    sku TEXT PRIMARY KEY, variants TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS metadata (
                    id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS bindings (
                    item_id TEXT PRIMARY KEY, code TEXT NOT NULL, color TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS faqs (
                    id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL);
            """)

    def _validate_paths(self):
        # Validate the supplied path before resolution; resolution hides links.
        for path in (self.account_dir, *self.account_dir.parents):
            if path.is_symlink():
                raise MaterialKnowledgeError("知识库目录不得使用符号链接")
        for suffix in ("", "-journal", "-wal", "-shm"):
            path = Path(str(self.path) + suffix)
            if path.is_symlink():
                raise MaterialKnowledgeError("知识库文件不得使用符号链接")
            if path.exists() and (not path.is_file() or path.stat().st_nlink != 1):
                raise MaterialKnowledgeError("知识库文件类型不安全")

    @contextmanager
    def _connection(self):
        self._validate_paths()
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self.path, flags, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode) or os.fstat(fd).st_nlink != 1:
                raise MaterialKnowledgeError("知识库文件类型不安全")
            if hasattr(os, "fchmod"):
                os.fchmod(fd, 0o600)
            else:  # Windows containers use Linux; native Windows has chmod.
                os.chmod(self.path, 0o600)
        finally:
            os.close(fd)
        conn = sqlite3.connect(str(self.path), timeout=10)
        try:
            conn.execute("PRAGMA journal_mode=DELETE")
            with conn:
                yield conn
        finally:
            conn.close()
            if self.path.exists() and not self.path.is_symlink():
                os.chmod(self.path, 0o600)

    def import_xlsx(self, content: bytes, filename: str) -> dict:
        if not isinstance(content, bytes) or not content or len(content) > MAX_UPLOAD_BYTES:
            raise MaterialKnowledgeError("Excel 文件为空或超过 10MB 限制")
        filename = _text(filename)
        if (not filename.lower().endswith(".xlsx") or "/" in filename
                or "\\" in filename or len(filename) > 200):
            raise MaterialKnowledgeError("仅支持不含路径的 .xlsx 文件名")
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                members = archive.infolist()
                if len(members) > 1000 or sum(m.file_size for m in members) > MAX_UNCOMPRESSED_BYTES:
                    raise MaterialKnowledgeError("Excel 解压大小超过限制")
                for member in members:
                    if (member.flag_bits & 1 or ".." in Path(member.filename).parts
                            or member.filename.startswith(("/", "\\"))):
                        raise MaterialKnowledgeError("Excel 压缩包内容无效")
                if "[Content_Types].xml" not in archive.namelist():
                    raise MaterialKnowledgeError("文件不是有效的 Excel 工作簿")
            workbook = openpyxl.load_workbook(io.BytesIO(content), read_only=True,
                                             data_only=True, keep_links=False)
        except MaterialKnowledgeError:
            raise
        except Exception as exc:
            raise MaterialKnowledgeError("无法读取 Excel 工作簿") from exc
        groups: dict[str, list[dict]] = {}
        imported_rows = 0
        try:
            for sheet in workbook.worksheets:
                # Ignore adversarial or stale dimension metadata and enforce our
                # own row budget while streaming the actual worksheet cells.
                sheet.reset_dimensions()
                mapping = None
                nonempty = False
                for row_number, values in enumerate(sheet.iter_rows(values_only=True), 1):
                    if len(values) > 100:
                        raise MaterialKnowledgeError("Excel 列数超过 100 限制")
                    if row_number > MAX_ROWS + 20:
                        raise MaterialKnowledgeError("Excel 行数超过 10000 限制")
                    if any(len(_text(v)) > MAX_CELL_CHARS for v in values):
                        raise MaterialKnowledgeError("Excel 单元格内容过长")
                    if not any(_text(v) for v in values):
                        continue
                    nonempty = True
                    if mapping is None:
                        headers = [_text(v) for v in values]
                        if REQUIRED_HEADERS <= set(headers):
                            recognized = [h for h in headers if h in HEADERS]
                            if len(recognized) != len(set(recognized)):
                                raise MaterialKnowledgeError(f"工作表 {sheet.title} 的表头重复")
                            mapping = {HEADERS[h]: i for i, h in enumerate(headers) if h in HEADERS}
                        elif row_number >= 20:
                            raise MaterialKnowledgeError(f"工作表 {sheet.title} 缺少必需表头：货号、鞋面、内里、鞋垫、鞋底")
                        continue
                    record = {field: _text(values[index]) if index < len(values) else ""
                              for field, index in mapping.items()}
                    for field in HEADERS.values():
                        record.setdefault(field, "")
                    if not record["sku"]:
                        continue
                    record["sku"] = normalize_code(record["sku"])
                    record["style_code"] = normalize_code(record["style_code"])
                    record["sources"] = [{"sheet": sheet.title, "row": row_number}]
                    imported_rows += 1
                    if imported_rows > MAX_ROWS:
                        raise MaterialKnowledgeError("Excel 商品记录超过 10000 限制")
                    variants = groups.setdefault(record["sku"], [])
                    existing = next((v for v in variants if all(
                        v[f] == record[f] for f in HEADERS.values())), None)
                    if existing:
                        existing["sources"].extend(record["sources"])
                    else:
                        variants.append(record)
                if nonempty and mapping is None:
                    raise MaterialKnowledgeError(f"工作表 {sheet.title} 缺少必需表头：货号、鞋面、内里、鞋垫、鞋底")
        except MaterialKnowledgeError:
            raise
        except Exception as exc:
            raise MaterialKnowledgeError("Excel 内容读取失败，原知识库保持不变") from exc
        finally:
            workbook.close()
        if not groups:
            raise MaterialKnowledgeError("Excel 中没有有效货号记录")
        result = {
            "version": hashlib.sha256(content).hexdigest(), "filename": filename,
            "imported_at": datetime.now(timezone.utc).isoformat(),
            "rows": imported_rows, "sku_count": len(groups),
            "record_count": sum(len(v) for v in groups.values()),
            "duplicates_merged": imported_rows - sum(len(v) for v in groups.values()),
            "conflict_count": sum(self._conflicting(v) for v in groups.values()),
            "empty_count": sum(not any(r[f] for f in MATERIAL_FIELDS)
                               for vs in groups.values() for r in vs),
        }
        result.update(code_count=result["sku_count"], row_count=result["rows"],
                      file_name=result["filename"])
        with self._connection() as conn:
            conn.execute("DELETE FROM records")
            conn.executemany("INSERT INTO records VALUES (?,?)", [
                (sku, json.dumps(variants, ensure_ascii=False)) for sku, variants in groups.items()])
            faq_row = conn.execute("SELECT payload FROM faqs WHERE id=1").fetchone()
            faqs = self._normalize_faqs(json.loads(faq_row[0])) if faq_row else []
            result.update(faq_count=len(faqs), enabled_faq_count=sum(faq["enabled"] for faq in faqs))
            conn.execute("INSERT OR REPLACE INTO metadata VALUES (1,?)",
                         (json.dumps(result, ensure_ascii=False),))
        return result

    @staticmethod
    def _conflicting(variants):
        return len({tuple(v[f] for f in MATERIAL_FIELDS) for v in variants}) > 1

    def status(self) -> dict:
        with self._connection() as conn:
            row = conn.execute("SELECT payload FROM metadata WHERE id=1").fetchone()
            faq_row = conn.execute("SELECT payload FROM faqs WHERE id=1").fetchone()
        faqs = self._normalize_faqs(json.loads(faq_row[0])) if faq_row else []
        result = json.loads(row[0]) if row else {
            "version": "", "filename": "", "rows": 0, "sku_count": 0,
            "record_count": 0, "conflict_count": 0, "empty_count": 0,
            "duplicates_merged": 0, "code_count": 0, "row_count": 0,
            "file_name": "", "imported_at": ""}
        return {**result, "faq_count": len(faqs), "enabled_faq_count": sum(faq["enabled"] for faq in faqs)}

    def reply_version(self) -> str:
        """Hash one consistent catalogue/binding snapshot for send validation.

        Python's SQLite context manager does not start a transaction for SELECT.
        An explicit read transaction keeps the metadata and binding queries on
        the same snapshot when an import or manual binding changes concurrently.
        File names and import times do not affect this logical reply version.
        """
        with self._connection() as conn:
            conn.execute("BEGIN")
            row = conn.execute("SELECT payload FROM metadata WHERE id=1").fetchone()
            version = json.loads(row[0]).get("version", "") if row else ""
            bindings = conn.execute(
                "SELECT item_id,code,color FROM bindings ORDER BY item_id"
            ).fetchall()
            faq_row = conn.execute("SELECT payload FROM faqs WHERE id=1").fetchone()
            faqs = self._normalize_faqs(json.loads(faq_row[0])) if faq_row else []
            catalog = []
            catalog_ref = ""
            source = self.account_dir / "shop_snapshot.json"
            try:
                descriptor = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            except FileNotFoundError:
                descriptor = None
            if descriptor is not None:
                with os.fdopen(descriptor, "rb") as file:
                    info = os.fstat(file.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 16 * 1024 * 1024:
                        raise MaterialKnowledgeError("商品快照类型或大小无效")
                    data = json.loads(file.read(16 * 1024 * 1024 + 1))
                if not isinstance(data, dict) or not isinstance(data.get("products"), list):
                    raise MaterialKnowledgeError("商品快照格式无效")
                catalog_ref = str(data.get("account_ref", ""))
                fields = ("id", "title", "description", "codes", "merchant_code", "model_code", "color", "sku_check", "image_urls")
                catalog = sorted([{key: item.get(key) for key in fields} for item in data["products"] if isinstance(item, dict)], key=lambda item: str(item.get("id", "")))
            snapshot = {"version": version, "bindings": bindings, "catalog": catalog, "catalog_ref": catalog_ref,
                        "faqs": sorted(faqs, key=lambda value: value["id"])}
            payload = json.dumps(snapshot, ensure_ascii=False, sort_keys=True,
                                 separators=(",", ":")).encode("utf-8")
            return hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _normalize_faqs(faqs: list) -> list[dict]:
        if not isinstance(faqs, list) or len(faqs) > 50:
            raise MaterialKnowledgeError("FAQ 必须是最多 50 条的列表", "faq_invalid")
        result, seen = [], set()
        for faq in faqs:
            if not isinstance(faq, dict) or set(faq) - {"id", "question", "answer", "enabled", "topics", "fixed_reply"}:
                raise MaterialKnowledgeError("FAQ 字段无效", "faq_invalid")
            identifier = faq.get("id")
            if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", identifier) or identifier in seen:
                raise MaterialKnowledgeError("FAQ ID 无效或重复", "faq_invalid")
            question, answer = faq.get("question"), faq.get("answer")
            if not knowledge_text_safe(question, 300) or not knowledge_text_safe(answer, 1500):
                raise MaterialKnowledgeError("FAQ 问答为空、过长或含不安全内容", "faq_unsafe")
            fixed_reply = faq.get("fixed_reply", False)
            if not isinstance(fixed_reply, bool):
                raise MaterialKnowledgeError("FAQ 固定回复状态无效", "faq_invalid")
            enabled = faq.get("enabled", False)
            topics = faq.get("topics", [])
            if not isinstance(enabled, bool) or not isinstance(topics, list) or len(topics) > 8:
                raise MaterialKnowledgeError("FAQ 启用状态或主题无效", "faq_invalid")
            if any(not knowledge_text_safe(topic, 40) for topic in topics):
                raise MaterialKnowledgeError("FAQ 主题为空、过长或含不安全内容", "faq_invalid")
            seen.add(identifier)
            result.append({"id": identifier, "question": question.strip(), "answer": answer.strip(),
                           "enabled": enabled, "topics": sorted(set(topic.strip() for topic in topics))})
            if fixed_reply:
                result[-1]["fixed_reply"] = True
        return result

    def get_faqs(self) -> list[dict]:
        with self._connection() as conn:
            row = conn.execute("SELECT payload FROM faqs WHERE id=1").fetchone()
        return self._normalize_faqs(json.loads(row[0])) if row else []

    def save_faqs(self, faqs: list) -> list[dict]:
        normalized = self._normalize_faqs(faqs)
        with self._connection() as conn:
            conn.execute("INSERT OR REPLACE INTO faqs VALUES (1,?)",
                         (json.dumps(normalized, ensure_ascii=False),))
        return normalized

    def _records(self):
        with self._connection() as conn:
            rows = conn.execute("SELECT variants FROM records ORDER BY sku").fetchall()
        return [record for row in rows for record in json.loads(row[0])]

    def search(self, query: str, limit: int = 30) -> list:
        query = normalize_code(query)
        limit = max(1, min(int(limit), 100))
        records = self._records()
        variants_by_sku = {}
        for record in records:
            variants_by_sku.setdefault(record["sku"], []).append(record)
        conflicts = {sku for sku, variants in variants_by_sku.items() if self._conflicting(variants)}
        return [dict(record, status="conflict" if record["sku"] in conflicts else (
            "matched" if any(record[f] for f in MATERIAL_FIELDS) else "empty"))
                for record in records if not query or any(query in normalize_code(record[f])
                    for f in ("sku", "style_code", "color", "category"))][:limit]

    def mentioned_codes(self, text: str) -> list[str]:
        """Return only exact known SKU/style mentions, including numeric codes.

        This shares lookup's NFKC/dash/whitespace normalization and identifier
        boundaries. Callers can detect a buyer referring to a different item
        without asking the model to choose an identifier.
        """
        records = self._records()
        known = {record["sku"] for record in records} | {
            record["style_code"] for record in records if record["style_code"]}
        return sorted(_found_codes(text, known))

    @staticmethod
    def _result(code, status, candidates, record=None):
        def with_status(value):
            return dict(value, status="conflict" if status == "conflict" else (
                "matched" if any(value[f] for f in MATERIAL_FIELDS) else "empty"))
        return {"code": code, "status": status,
                "record": with_status(record) if record else None,
                "candidates": [with_status(value) for value in candidates[:30]],
                "candidate_count": len(candidates), "has_more": len(candidates) > 30}

    def lookup(self, code: str, color: str = "") -> dict:
        code, color = normalize_code(code), _text(color)
        records = self._records()
        # A full SKU is more specific than a style identifier.
        exact = [r for r in records if r["sku"] == code]
        candidates = exact or [r for r in records if r["style_code"] == code and code]
        if not candidates:
            return self._result(code, "not_found", [])
        relevant_skus = {r["sku"] for r in candidates}
        if color:
            candidates = [r for r in candidates if r["color"] == color]
            if not candidates:
                return self._result(code, "not_found", [])
        for sku in {r["sku"] for r in candidates} & relevant_skus:
            # A colour hint must not hide a conflicting row for the same SKU.
            all_variants = [r for r in records if r["sku"] == sku]
            if self._conflicting(all_variants):
                return self._result(code, "conflict", all_variants)
        if len(candidates) != 1:
            return self._result(code, "ambiguous", candidates)
        record = candidates[0]
        return self._result(code, "matched" if any(record[f] for f in MATERIAL_FIELDS)
                            else "empty", candidates, record)

    def bind_product(self, item_id: str, code: str, color: str = "") -> dict:
        item_id = _identifier(item_id, "商品 ID")
        code = normalize_code(_identifier(code, "货号"))
        color = _text(color)
        result = self.lookup(code, color)
        if result["status"] == "not_found":
            raise MaterialKnowledgeError("绑定编码或颜色不在当前知识库中")
        with self._connection() as conn:
            conn.execute("INSERT OR REPLACE INTO bindings VALUES (?,?,?)", (item_id, code, color))
        return dict(result, item_id=item_id, color=color)

    def product_binding(self, item_id: str) -> dict | None:
        item_id = _identifier(item_id, "商品 ID")
        with self._connection() as conn:
            row = conn.execute("SELECT code,color FROM bindings WHERE item_id=?", (item_id,)).fetchone()
        if not row:
            return None
        result = self.lookup(row[0], row[1])
        return {"item_id": item_id, "code": row[0], "color": row[1],
                "status": result["status"]}

    def unbind_product(self, item_id: str) -> dict:
        item_id = _identifier(item_id, "商品 ID")
        with self._connection() as conn:
            cursor = conn.execute("DELETE FROM bindings WHERE item_id=?", (item_id,))
        return {"item_id": item_id, "removed": bool(cursor.rowcount)}

    def resolve_product(self, facts: dict, buyer_text: str = "", history: list | None = None) -> dict:
        item_id = facts.get("item_id") or facts.get("id")
        binding = self.product_binding(str(item_id)) if item_id else None
        records = self._records()
        if binding:
            base = self.lookup(binding["code"], binding["color"])
            if binding["color"] or base["status"] != "ambiguous":
                return base
        else:
            check = facts.get("sku_check")
            if isinstance(check, dict) and check.get("version") == 1:
                selected = check.get("selected_code")
                if not selected:
                    return self._result("", "ambiguous" if check.get("status") == "ambiguous" else "not_found", [])
                base = self.lookup(selected, facts.get("color", ""))
                if base["status"] != "ambiguous":
                    return base
            else:
                known = {r["sku"] for r in records} | {r["style_code"] for r in records if r["style_code"]}
                explicit = [facts.get(key, "") for key in ("merchant_code", "model_code")]
                codes = facts.get("codes", [])
                if isinstance(codes, str):
                    codes = [codes]
                explicit.extend(codes or [])
                matched = {code for value in explicit for code in _found_codes(value, known)}
                if not matched:
                    matched = {code for key in ("title", "description", "desc")
                               for code in _found_codes(facts.get(key, ""), known)}
                if not matched:
                    return self._result("", "not_found", [])
                # Ignore a style identifier when the same facts identify its SKU.
                selected = {r["sku"] for r in records if r["sku"] in matched}
                selected_styles = {r["style_code"] for r in records if r["sku"] in selected}
                matched -= selected_styles - selected
                if len(matched) != 1:
                    candidates = [r for r in records if r["sku"] in matched or r["style_code"] in matched]
                    return self._result("", "ambiguous", candidates)
                base = self.lookup(next(iter(matched)), facts.get("color", ""))
                if base["status"] != "ambiguous":
                    return base
        candidates = base["candidates"]
        if base.get("has_more"):
            return base
        # Only buyer/user history can clarify colour; seller replies are not a
        # source of authoritative product identity. Most recent hint wins.
        texts = [buyer_text]
        for message in reversed(history or []):
            if isinstance(message, dict) and message.get("role") in ("user", "buyer"):
                texts.append(message.get("content", message.get("text", "")))
        for text in texts:
            scoped_skus = {r["sku"] for r in candidates}
            # A hint mentioning another style must not influence this item.
            known = {r["sku"] for r in records} | {r["style_code"] for r in records if r["style_code"]}
            allowed = scoped_skus | {r["style_code"] for r in candidates if r["style_code"]}
            if set(_found_codes(text, known)) - allowed:
                return base
            sku_hits = _found_codes(text, scoped_skus)
            if len(sku_hits) == 1:
                return self.lookup(sku_hits[0])
            if len(sku_hits) > 1:
                return base
            colours = sorted({r["color"] for r in candidates if r["color"]}, key=len, reverse=True)
            remaining = _text(text)
            hits = []
            for colour in colours:
                if colour in remaining:
                    hits.append(colour)
                    remaining = remaining.replace(colour, "")
            if len(hits) == 1:
                return self.lookup(base["code"], hits[0])
            if hits:
                return base
        return base
