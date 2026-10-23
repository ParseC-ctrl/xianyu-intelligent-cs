"""Low-frequency, read-only Xianyu account and product synchronization."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import os
import re
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import unicodedata
from datetime import datetime, timezone

from account_storage import AccountStorage, AccountStorageError, DEFAULT_ACCOUNT_ID, normalize_account_key
from platform_profile import browser_headers


APP_KEY = "34839810"
MTOP_HOST = "https://h5api.m.goofish.com"
PROFILE_API = "mtop.taobao.idlemessage.pc.loginuser.get"
PRODUCTS_API = "mtop.idle.web.xyh.item.list"
PAGE_SIZE = 20
MAX_ITEMS = 100
MAX_PAGES = 5
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
REQUEST_TIMEOUT = max(3, min(int(os.environ.get("SAAS_SHOP_SYNC_REQUEST_TIMEOUT_SECONDS", "8")), 15))
REQUEST_INTERVAL = max(0.2, min(float(os.environ.get("SAAS_SHOP_SYNC_REQUEST_INTERVAL_SECONDS", "0.5")), 5.0))
SYNC_MAX_SECONDS = max(15, min(int(os.environ.get("SAAS_SHOP_SYNC_MAX_SECONDS", "55")), 65))
SYNC_COOLDOWN_SECONDS = max(1, int(os.environ.get("SAAS_SHOP_SYNC_COOLDOWN_SECONDS", "60")))
TENANTS_ROOT = os.environ.get("SAAS_TENANTS_DIR", "/var/lib/xianyu-intelligent-cs/tenants")
CIRCUIT_PATH = os.path.join(os.path.dirname(TENANTS_ROOT), "shop-sync-circuit.json")
SNAPSHOT_NAME = "shop_snapshot.json"
SYNC_STATE_NAME = "shop_sync_state.json"
SYNC_STATE_VERSION = 1

# These values are deliberately small and stable: they are exposed to the
# browser as connection state and never contain platform response text or a
# Cookie value.
SYNC_STATUS_CATALOG = {
    "unconfigured": {
        "label": "未连接",
        "message": "还没有连接闲鱼店铺",
        "action": "请粘贴 Cookie 并开始检测",
    },
    "pending": {
        "label": "待检测",
        "message": "Cookie 已保存，等待完成检测",
        "action": "点击重新检测",
    },
    "verified": {
        "label": "已验证",
        "message": "Cookie 已验证（不显示内容）",
        "action": "可随时重新检测店铺商品",
    },
    "risk_control": {
        "label": "接口请求受限",
        "message": "最近一次自动连接请求未通过接口校验或被限制，尚不能确认是否需要安全验证。",
        "action": "稍后重新检测；仅在闲鱼明确提示时处理验证。",
    },
    "verification_required": {
        "label": "接口要求验证",
        "message": "自动连接接口返回了明确的验证要求，请在对应的闲鱼官方页面按提示处理；App 不一定弹窗。",
        "action": "按官方页面提示处理后重新检测",
    },
    "account_restricted": {
        "label": "账号受限",
        "message": "闲鱼限制了当前账号的部分操作，暂时不能发布商品",
        "action": "请在闲鱼官方页面查看处理通知，处理后重新检测",
    },
    "risk_cooldown": {
        "label": "请求保护冷却中",
        "message": "系统因之前的受限请求暂停了检测，请等待冷却结束后再试。此为本地保护，不是平台账号通知。",
        "action": "等待冷却结束后重新检测",
    },
    "cookie_expired": {
        "label": "Cookie 已失效",
        "message": "Cookie 已失效，请重新登录闲鱼并重新获取 Cookie",
        "action": "重新获取完整 Cookie 后粘贴",
    },
    "cookie_invalid": {
        "label": "Cookie 格式有误",
        "message": "Cookie 格式无效，请重新复制完整 Cookie",
        "action": "重新复制完整 Cookie 后粘贴",
    },
    "cookie_incomplete": {
        "label": "需要重新获取",
        "message": "Cookie 缺少登录信息，请重新复制完整 Cookie",
        "action": "重新获取完整 Cookie 后粘贴",
    },
    "sync_error": {
        "label": "检测失败",
        "message": "暂时无法确认 Cookie 状态，请稍后重新检测",
        "action": "稍后重新检测",
    },
    "bot_running": {
        "label": "自动客服运行中",
        "message": "请先暂停自动客服，再更新店铺 Cookie",
        "action": "暂停后重新检测",
    },
    "sync_cooldown": {
        "label": "操作太频繁",
        "message": "操作太频繁，请稍后再检测",
        "action": "稍后再检测",
    },
    "sync_busy": {
        "label": "正在检测",
        "message": "已有店铺检测正在进行，请稍后再试",
        "action": "等待当前检测完成",
    },
    "sync_timeout": {
        "label": "检测超时",
        "message": "店铺商品较多，检测超时，请稍后再试",
        "action": "稍后重新检测",
    },
    "profile_missing": {
        "label": "账号信息不完整",
        "message": "已连接账号，但没有读取到闲鱼昵称，请稍后再检测",
        "action": "稍后重新检测",
    },
    "platform_error": {
        "label": "闲鱼暂时不可用",
        "message": "闲鱼暂时无法识别该账号，请稍后再检测",
        "action": "稍后重新检测",
    },
    "network_error": {
        "label": "网络检测失败",
        "message": "暂时无法连接闲鱼，请稍后再检测",
        "action": "稍后重新检测",
    },
    "platform_busy": {
        "label": "闲鱼请求繁忙",
        "message": "闲鱼当前请求繁忙，系统会降低频率后再试",
        "action": "请稍后重新检测",
    },
}
# Historical platform wording must not override these evidence-bounded labels.
CANONICAL_STATUS_CODES = frozenset({"risk_control", "verification_required", "risk_cooldown"})
PERSISTED_SYNC_CODES = frozenset(
    {
        "risk_control",
        "verification_required",
        "account_restricted",
        "risk_cooldown",
        "cookie_expired",
        "cookie_invalid",
        "cookie_incomplete",
    }
)

ACCOUNT_RESTRICTION_MARKERS = (
    "ACCOUNT_BANNED",
    "USER_BANNED",
    "PUBLISH_FORBIDDEN",
    "ITEM_PUBLISH_FORBIDDEN",
    "VIOLATION",
    "违规",
    "限制发布",
    "禁止发布",
)
RISK_CONTROL_MARKERS = (
    "RGV587",
    "USER_VALIDATE",
    "USER_VALID",
    "LOGIN_CHECK",
    "/PUNISH",
)
# Exact codes, not arbitrary error-code substrings such as NOT_CAPTCHA_ERROR.
VERIFICATION_CODES = frozenset({
    "CAPTCHA", "CAPTCHA_REQUIRED", "FAIL_SYS_CAPTCHA", "FAIL_SYS_CAPTCHA_REQUIRED",
    "SECURITY_CHECK", "SECURITY_CHECK_REQUIRED", "FAIL_SYS_SECURITY_CHECK",
    "FAIL_SYS_SECURITY_CHECK_REQUIRED",
})
VERIFICATION_REQUEST = re.compile(
    r"(?<![不无未])(?:请|需要|必须)(?:先)?(?:完成|进行)(?:安全验证|人机验证|滑块验证|验证码验证)"
    r"|(?<!NO )(?<!NOT )\b(?:CAPTCHA|SECURITY CHECK) (?:IS )?REQUIRED\b"
)
ERROR_STATUS_FIELDS = (
    "ret", "code", "errorCode", "error_code", "retCode", "status", "statusCode",
    "status_code", "message", "msg", "errorMsg", "errorMessage",
)
VERIFICATION_FIELDS = ("captchaRequired", "needCaptcha", "securityCheckRequired", "verificationRequired")
SESSION_EXPIRED_MARKERS = (
    "SESSION_EXPIRED",
    "SESSION INVALID",
    "TOKEN_EX",
    "TOKEN_EMPTY",
    "ILLEGAL_ACCESS",
    "LOGIN_EXPIRED",
    "NOT_LOGIN",
    "AUTH_EXPIRED",
    "AUTH_INVALID",
)
PLATFORM_BUSY_MARKERS = (
    "FAIL_SYS_BUSY",
    "SYSTEM_BUSY",
    "SERVER_BUSY",
    "TOO_MANY_REQUEST",
    "RATE_LIMIT",
    "FREQUENCY_LIMIT",
    "TRAFFIC_LIMIT",
    "被挤爆",
)

_request_lock = threading.Lock()
_last_request_at = 0.0
_sync_lock = threading.Lock()
_last_sync_by_tenant: dict[tuple[int, str], float] = {}
_sync_gate = threading.Lock()


def _account_storage() -> AccountStorage:
    return AccountStorage(TENANTS_ROOT)


def _account_root(user_id: int, account_key: str | None = DEFAULT_ACCOUNT_ID, *, create: bool = False) -> str:
    storage = _account_storage()
    try:
        path = (
            storage.ensure_account_dir(user_id, account_key)
            if create
            else storage.account_dir(user_id, account_key)
        )
    except AccountStorageError as error:
        raise OSError("invalid account storage path") from error
    return str(path)


class ShopSyncError(RuntimeError):
    """A safe synchronization failure that never contains a platform response."""

    def __init__(self, code: str, message: str, retry_after: float | None = None):
        self.code = code
        self.retry_after = (
            max(0, math.ceil(retry_after))
            if retry_after is not None and math.isfinite(retry_after) else None
        )
        super().__init__(message)


def sync_status_payload(code: str, message: str | None = None, checked_at: str = "") -> dict:
    """Return a bounded, browser-safe description of a sync status."""
    candidate = str(code or "")
    normalized = candidate if re.fullmatch(r"[a-z][a-z0-9_]{1,48}", candidate) else "sync_error"
    catalog = SYNC_STATUS_CATALOG.get(normalized, SYNC_STATUS_CATALOG["sync_error"])
    if normalized in CANONICAL_STATUS_CODES:
        message = catalog["message"]
    return {
        "code": normalized,
        "label": catalog["label"],
        "message": (message or catalog["message"])[:240],
        "action": catalog["action"],
        "checked_at": str(checked_at or "")[:40],
    }


def save_sync_state(
    user_id: int,
    code: str,
    message: str | None = None,
    checked_at: str = "",
    account_ref_value: str = "",
    account_key: str = DEFAULT_ACCOUNT_ID,
) -> None:
    """Persist only non-secret connection state for the tenant."""
    status = sync_status_payload(code, message, checked_at or time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    payload = {
        "version": SYNC_STATE_VERSION,
        "code": status["code"],
        "message": status["message"],
        "checked_at": status["checked_at"],
        "account_ref": str(account_ref_value or "")[:64],
    }
    root = _account_root(user_id, account_key, create=True)
    _account_storage().atomic_write_path(os.path.join(root, SYNC_STATE_NAME), json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def load_sync_state(user_id: int, account_key: str = DEFAULT_ACCOUNT_ID) -> dict | None:
    """Load validated connection state; malformed state fails closed."""
    try:
        path = os.path.join(_account_root(user_id, account_key), SYNC_STATE_NAME)
    except OSError:
        return None
    try:
        with open(path, encoding="utf-8") as file:
            payload = json.load(file)
    except (OSError, TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("version") != SYNC_STATE_VERSION:
        return None
    code = payload.get("code")
    if code not in SYNC_STATUS_CATALOG:
        return None
    status = sync_status_payload(code, payload.get("message"), payload.get("checked_at", ""))
    status["account_ref"] = str(payload.get("account_ref") or "")[:64]
    return status


def parse_cookie_header(value: str) -> tuple[str, dict[str, str]]:
    if not isinstance(value, str):
        raise ShopSyncError("cookie_invalid", "Cookie 格式无效")
    normalized = value.strip()
    if normalized.lower().startswith("cookie:"):
        normalized = normalized[7:].strip()
    if not normalized or "\r" in normalized or "\n" in normalized:
        raise ShopSyncError("cookie_invalid", "Cookie 格式无效")

    cookies: dict[str, str] = {}
    for part in normalized.split(";"):
        name, separator, cookie_value = part.strip().partition("=")
        if not separator or not name:
            continue
        if not name.isascii() or any(character.isspace() for character in name):
            raise ShopSyncError("cookie_invalid", "Cookie 格式无效")
        cookies[name] = cookie_value.strip()

    user_id = cookies.get("unb", "")
    token_value = cookies.get("_m_h5_tk", "")
    token = token_value.split("_", 1)[0]
    if (
        not user_id.isascii()
        or not user_id.isdigit()
        or len(user_id) > 64
        or not token
        or len(token) > 256
        or not token.isascii()
    ):
        raise ShopSyncError("cookie_incomplete", "Cookie 缺少有效的登录信息，请重新复制完整 Cookie")
    return normalized, cookies


def account_ref(cookies: dict[str, str]) -> str:
    return hashlib.sha256(cookies["unb"].encode("utf-8")).hexdigest()[:16]


def reserve_sync(user_id: int, account_key: str = DEFAULT_ACCOUNT_ID) -> None:
    """Apply a per-tenant cooldown before using the shared platform egress."""
    now = time.monotonic()
    with _sync_lock:
        scope = (int(user_id), normalize_account_key(account_key))
        previous = _last_sync_by_tenant.get(scope, 0.0)
        if previous and now - previous < SYNC_COOLDOWN_SECONDS:
            raise ShopSyncError("sync_cooldown", "操作太频繁，请稍后再试", SYNC_COOLDOWN_SECONDS - (now - previous))
        _last_sync_by_tenant[scope] = now


def note_sync(user_id: int, account_key: str = DEFAULT_ACCOUNT_ID) -> None:
    """Record a completed explicit sync without enforcing the cooldown.

    The user-initiated login replace must never be blocked by the anti-abuse
    cooldown, but later background syncs should still be cooled down.
    """
    now = time.monotonic()
    with _sync_lock:
        _last_sync_by_tenant[(int(user_id), normalize_account_key(account_key))] = now


def _safe_text(value, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    value = " ".join(value.split()).strip()
    return value[:limit]


# The profile endpoint has returned several equivalent shapes over time. Keep
# the traversal deliberately allow-listed so a product/card field can never
# accidentally become the seller identity.
_PROFILE_CONTAINER_KEYS = (
    "data",
    "userInfo",
    "userInfoModel",
    "currentUser",
    "profile",
    "user",
    "account",
    "result",
)
_PROFILE_NICK_KEYS = (
    "nick",
    "nickname",
    "nickName",
    "userNick",
    "userNickname",
    "userNickName",
    "fishNick",
    "nicknameStr",
    "displayName",
)
_PROFILE_ID_KEYS = ("userId", "user_id", "userIdStr", "accountId", "uid")


def _profile_nodes(value):
    """Yield bounded, allow-listed profile dictionaries from mtop data."""
    pending = [value]
    visited = set()
    while pending and len(visited) < 16:
        node = pending.pop(0)
        if not isinstance(node, dict) or id(node) in visited:
            continue
        visited.add(id(node))
        yield node
        for key in _PROFILE_CONTAINER_KEYS:
            child = node.get(key)
            if isinstance(child, dict):
                pending.append(child)
            elif isinstance(child, str) and len(child) <= MAX_RESPONSE_BYTES:
                try:
                    decoded = json.loads(child)
                except (TypeError, ValueError):
                    continue
                if isinstance(decoded, dict):
                    pending.append(decoded)


def _profile_text(value, keys, limit: int) -> str:
    for node in _profile_nodes(value):
        for key in keys:
            candidate = _safe_text(node.get(key), limit)
            if candidate:
                return candidate
    return ""


def _profile_id(value) -> str:
    for node in _profile_nodes(value):
        for key in _PROFILE_ID_KEYS:
            candidate = node.get(key)
            if candidate in (None, "") or isinstance(candidate, bool):
                continue
            normalized = str(candidate).strip()
            if normalized.isascii() and normalized.isdigit() and len(normalized) <= 64:
                return normalized
    return ""


def _cookie_nickname(cookies: dict[str, str]) -> str:
    """Use only the platform's display-name cookies as a bounded fallback."""
    for key in ("tracknick", "lgc", "nick"):
        raw = cookies.get(key, "")
        if not isinstance(raw, str) or not raw:
            continue
        candidate = _safe_text(urllib.parse.unquote_plus(raw), 80)
        if candidate:
            return candidate
    return ""


def _price_value(value) -> str:
    if isinstance(value, bool) or value is None:
        return ""
    text = str(value).strip().replace(",", "")
    text = re.sub(r"^[^0-9.+-]+", "", text)
    match = re.match(r"^[+]?(\d+(?:\.\d+)?)", text)
    return match.group(1) if match else ""


def _image_url(value) -> str:
    if not isinstance(value, str):
        return ""
    candidate = value.strip()
    if candidate.startswith("//"):
        candidate = "https:" + candidate
    if len(candidate) > 2048 or "\\" in candidate or any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in candidate):
        return ""
    try:
        parsed = urllib.parse.urlsplit(candidate)
        host, port = parsed.hostname, parsed.port
    except ValueError:
        return ""
    if (parsed.scheme not in {"http", "https"} or not host or parsed.username is not None or parsed.password is not None
            or port not in {None, 80, 443}):
        return ""
    host = host.rstrip(".").lower()
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".lan", ".home")):
        return ""
    try:
        if not ipaddress.ip_address(host).is_global:
            return ""
    except ValueError:
        if "." not in host or not re.fullmatch(r"[a-z0-9.-]+", host) or re.fullmatch(r"[0-9.]+", host):
            return ""
    return candidate


def extract_product_images(card: dict) -> list[str]:
    """Retain platform image order, rejecting executable/local URL forms.

    The final valid picture is retained even when the platform supplies more
    than twenty; buyers often place the identifying label in that picture.
    No URL is fetched here. The consumer must also validate DNS/redirects.
    """
    collection_keys = {"images", "imageInfos", "imageList", "imageUrls", "image_urls", "pictures", "pictureList", "picList", "picUrls"}
    url_keys = ("url", "imageUrl", "picUrl", "image_url", "originalUrl", "originUrl", "src")
    urls, seen, visited = [], set(), 0
    def collect(value, depth=0):
        nonlocal visited
        if depth > 4 or visited >= 5000:
            return
        visited += 1
        if isinstance(value, str):
            url = _image_url(value)
            if url and url not in seen:
                seen.add(url); urls.append(url)
        elif isinstance(value, list):
            for child in value:
                collect(child, depth + 1)
        elif isinstance(value, dict):
            for key in url_keys:
                if _image_url(value.get(key)):
                    collect(value[key], depth + 1)
                    return
            for key, child in value.items():
                if key in collection_keys or key in {"list", "data"} or str(key).isdigit():
                    collect(child, depth + 1)
    for key, value in card.items():
        if key in collection_keys:
            collect(value)
    if not urls:
        url = _product_image_url(card)
        if url:
            urls.append(url)
    return urls if len(urls) <= 20 else urls[:19] + [urls[-1]]


def _product_image_url(card: dict) -> str:
    candidates = []
    pic_info = card.get("picInfo")
    if isinstance(pic_info, dict):
        candidates.append(pic_info.get("picUrl"))
    detail_params = card.get("detailParams")
    if isinstance(detail_params, dict):
        candidates.append(detail_params.get("picUrl"))
    candidates.extend(
        card.get(key)
        for key in ("image_url", "imageUrl", "picUrl")
    )
    for candidate in candidates:
        normalized = _image_url(candidate)
        if normalized:
            return normalized
    return ""


def _text_labels(card: dict) -> list[str]:
    labels: list[str] = []
    label_vo = card.get("itemLabelDataVO")
    label_data = label_vo.get("labelData") if isinstance(label_vo, dict) else None
    if not isinstance(label_data, dict):
        return labels
    for region in label_data.values():
        tags = region.get("tagList") if isinstance(region, dict) else None
        if not isinstance(tags, list):
            continue
        for tag in tags:
            data = tag.get("data") if isinstance(tag, dict) else None
            if not isinstance(data, dict) or data.get("type") == "img":
                continue
            content = _safe_text(data.get("content"), 60)
            if content and content not in labels:
                labels.append(content)
    return labels[:4]


def extract_product_codes(title, description, merchant_code="") -> list[str]:
    text = unicodedata.normalize("NFKC", str(title or "")[:MAX_RESPONSE_BYTES] + " " + str(description or "")[:MAX_RESPONSE_BYTES]).upper()
    codes = re.findall(r"(?<![A-Z0-9])((?:[A-Z]{1,8}[0-9][A-Z0-9]{2,24}|[0-9]{4,24})(?:[-.][A-Z0-9]{1,10})?)(?![A-Z0-9])", text)
    if merchant_code:
        codes.append(str(merchant_code).upper())
    return list(dict.fromkeys(codes))[:30]


def _listing_modified_at(card):
    for key in ("lastModified", "lastModifiedTime", "modifyTime", "modifiedTime", "updateTime", "gmtModified"):
        value = card.get(key)
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            continue
        text = str(value).strip()
        try:
            if re.fullmatch(r"[0-9]{10}|[0-9]{13}", text):
                moment = datetime.fromtimestamp(int(text) / (1000 if len(text) == 13 else 1), timezone.utc)
            else:
                moment = datetime.fromisoformat(text)
                if moment.tzinfo is None:
                    continue
            if moment.year >= 2000:
                return moment.astimezone(timezone.utc).isoformat()
        except (ValueError, OverflowError, OSError):
            continue
    return ""


def extract_product(card: dict, synced_at: str, *, description_limit: int = 240) -> dict | None:
    if not isinstance(card, dict):
        return None
    item_id = str(card.get("id") or card.get("itemId") or "").strip()
    if not item_id.isascii() or not item_id.isdigit() or len(item_id) > 64:
        return None

    summary = card.get("titleSummary")
    summary_title = summary.get("text") if isinstance(summary, dict) else ""
    raw_title = card.get("title") or summary_title
    title = _safe_text(raw_title, 160) or "未命名商品"
    price_info = card.get("priceInfo")
    price = _price_value(price_info.get("price") if isinstance(price_info, dict) else card.get("price"))
    raw_description = (
        card.get("desc")
        or card.get("description")
        or card.get("itemDesc")
        or ""
    )
    description = _safe_text(raw_description, description_limit)
    labels = _text_labels(card)
    status_code = str(card.get("itemStatus", ""))
    status_text = _safe_text(card.get("itemStatusStr"), 24)
    inactive_labels = {"已售出", "已卖出", "卖出", "已下架", "已删除", "草稿", "交易成功"}
    status = status_text if status_text in inactive_labels else {"0": "在售", "1": "已下架"}.get(status_code, status_text)
    product = {
        "id": item_id,
        "title": title,
        "description": description,
        "price": price,
        "status": status,
        "source": "cookie",
        "updated_at": synced_at,
        "description_source": "list" if description else "",
        "detail_sync_status": "pending",
        "detail_synced_at": "",
    }
    if labels:
        product["labels"] = labels
    # Preserve identifiers before truncating a long product description. The
    # lookup itself remains local and needs no extra platform detail request.
    merchant_code = card.get("merchantCode") or card.get("merchant_code") or card.get("outerId") or card.get("outer_id")
    normalized_merchant = _safe_text(str(merchant_code), 80) if isinstance(merchant_code, (str, int)) and not isinstance(merchant_code, bool) else ""
    product["list_merchant_code"] = normalized_merchant
    product["list_title_codes"] = extract_product_codes(raw_title, "")
    if normalized_merchant:
        product["merchant_code"] = normalized_merchant
        product["merchant_code_source"] = "list"
    codes = extract_product_codes(raw_title, raw_description, normalized_merchant)
    if codes:
        product["codes"] = codes
        product["codes_source"] = "listing"
    product["image_urls"] = extract_product_images(card)
    image_url = _product_image_url(card)
    if image_url:
        product["image_url"] = image_url
    # Only actual listing facts contribute. Labels, timestamps and cached
    # detail prose cannot turn an unchanged listing into a new detail request.
    product["list_modified_at"] = _listing_modified_at(card)
    identity = {"title": _safe_text(raw_title, MAX_RESPONSE_BYTES),
                "description": _safe_text(raw_description, MAX_RESPONSE_BYTES),
                "merchant_code": normalized_merchant, "price": price,
                "status": status, "image_url": image_url, "modified_at": product["list_modified_at"]}
    product["list_fingerprint"] = hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True,
                                                    separators=(",", ":")).encode()).hexdigest()
    return product


def _atomic_json(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".shop-sync-", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, separators=(",", ":"))
            file.flush()
            os.fsync(file.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def save_snapshot(user_id: int, payload: dict, account_key: str = DEFAULT_ACCOUNT_ID) -> None:
    """Persist a non-secret, account-bound product snapshot."""
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise ValueError("invalid shop snapshot")
    if os.environ.get("SAAS_CUSTOMER_SERVICE_ONLY") == "1":
        payload = active_catalog_snapshot(payload)
    root = _account_root(user_id, account_key, create=True)
    _account_storage().atomic_write_path(
        os.path.join(root, SNAPSHOT_NAME),
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
    )


def active_catalog_snapshot(snapshot):
    """Keep only explicitly published/on-sale local facts; no platform calls."""
    if not isinstance(snapshot, dict):
        return snapshot
    products = [p for p in snapshot.get("products", []) if isinstance(p, dict) and is_active_product(p)]
    result = {**snapshot, "products": products, "product_count": len(products), "active_only": True}
    if isinstance(snapshot.get("catalog_sync"), dict):
        receipt = dict(snapshot["catalog_sync"])
        for status in ("ready", "failed", "pending"):
            receipt[status] = sum(p.get("detail_sync_status", "pending") == status for p in products)
        receipt["remaining_pending"] = sum(p.get("detail_sync_status", "pending") == "pending" or bool(p.get("sku_check_pending")) for p in products)
        result["catalog_sync"] = receipt
    return result


def is_active_product(product):
    return str(product.get("status") or "").strip() in {"在售", "在卖", "已发布"}


def load_verified_snapshot(user_id: int, account_key: str = DEFAULT_ACCOUNT_ID) -> dict | None:
    """Return a snapshot only when it belongs to the currently stored Cookie."""
    try:
        root = _account_root(user_id, account_key)
    except OSError:
        return None
    try:
        with open(os.path.join(root, SNAPSHOT_NAME), encoding="utf-8") as file:
            snapshot = json.load(file)
        with open(os.path.join(root, "cookies.txt"), encoding="utf-8") as file:
            cookie_header = file.read().strip()
    except (OSError, TypeError, ValueError):
        return None
    if not isinstance(snapshot, dict) or snapshot.get("version") != 1:
        return None
    try:
        _, cookies = parse_cookie_header(cookie_header)
    except ShopSyncError:
        return None
    if snapshot.get("account_ref") != account_ref(cookies):
        return None
    if not isinstance(snapshot.get("nickname"), str) or not isinstance(snapshot.get("products"), list):
        return None
    return active_catalog_snapshot(snapshot) if os.environ.get("SAAS_CUSTOMER_SERVICE_ONLY") == "1" else snapshot


def clear_connection(user_id: int, account_key: str = DEFAULT_ACCOUNT_ID) -> None:
    """Drop the verified snapshot and sync state for one account.

    Used by the explicit "disconnect" action so a stopped shop no longer looks
    verified. Cookies are cleared separately by the caller.
    """
    try:
        root = _account_root(user_id, account_key)
    except OSError:
        return
    for name in (SNAPSHOT_NAME, SYNC_STATE_NAME):
        try:
            os.remove(os.path.join(root, name))
        except OSError:
            pass


def _circuit_until() -> float:
    try:
        with open(CIRCUIT_PATH, encoding="utf-8") as file:
            payload = json.load(file)
        return float(payload.get("until", 0)) if isinstance(payload, dict) else 0
    except (OSError, TypeError, ValueError):
        return 0


def check_sync_circuit() -> None:
    remaining = _circuit_until() - time.time()
    if remaining > 0:
        raise ShopSyncError("risk_cooldown", SYNC_STATUS_CATALOG["risk_cooldown"]["message"], remaining)


def _trip_circuit() -> None:
    _atomic_json(CIRCUIT_PATH, {"until": time.time() + 600})


def _failure_code_from_text(value: str) -> str:
    """Classify a platform failure without returning or logging its raw text."""
    text = str(value or "").upper()
    if any(marker in text for marker in ACCOUNT_RESTRICTION_MARKERS):
        return "account_restricted"
    if any(part.strip() in VERIFICATION_CODES for part in text.split("::")) or VERIFICATION_REQUEST.search(text):
        return "verification_required"
    if any(marker in text for marker in RISK_CONTROL_MARKERS):
        return "risk_control"
    if any(marker in text for marker in SESSION_EXPIRED_MARKERS):
        return "cookie_expired"
    if any(marker in text for marker in PLATFORM_BUSY_MARKERS):
        return "platform_busy"
    return "platform_error"


def _response_failure_code(raw: dict) -> str:
    """Inspect only response error fields and explicit verification flags."""
    nodes = [raw]
    if isinstance(raw.get("content"), dict):
        nodes.append(raw["content"])
    codes = set()
    for node in nodes:
        for key in ERROR_STATUS_FIELDS:
            value = node.get(key)
            values = value[:16] if isinstance(value, (list, tuple)) else [value]
            for text in values:
                if isinstance(text, str) and text.partition("::")[0].strip().upper() != "SUCCESS":
                    codes.add(_failure_code_from_text(text[:4096]))
    # A named boolean requirement is evidence; product text/URLs are not.
    flag_nodes = nodes + ([raw["data"]] if isinstance(raw.get("data"), dict) else [])
    if any(node.get(key) is True for node in flag_nodes for key in VERIFICATION_FIELDS):
        codes.add("verification_required")
    for code in ("account_restricted", "verification_required", "risk_control", "cookie_expired", "platform_busy"):
        if code in codes:
            return code
    return "platform_error"


def _raise_classified_failure(code: str) -> None:
    if code == "account_restricted":
        raise ShopSyncError(code, "闲鱼限制了当前账号的部分操作，暂时不能发布商品")
    if code in {"risk_control", "verification_required"}:
        _trip_circuit()
        raise ShopSyncError(code, SYNC_STATUS_CATALOG[code]["message"])
    if code == "cookie_expired":
        raise ShopSyncError(code, "Cookie 已失效，请重新登录闲鱼后复制完整 Cookie")
    if code == "platform_busy":
        raise ShopSyncError(code, "闲鱼当前请求繁忙，请稍后重试")
    raise ShopSyncError("platform_error", "闲鱼暂时无法识别该账号，请稍后重试")


def _classify_response(raw) -> dict:
    if not isinstance(raw, dict):
        raise ShopSyncError("platform_error", "闲鱼返回异常，请稍后重试")
    ret = raw.get("ret") or []
    if isinstance(ret, str):
        ret = [ret]
    code = _response_failure_code(raw)
    if code != "platform_error" or not (
        isinstance(ret, (list, tuple)) and ret
        and all(isinstance(item, str) and item.partition("::")[0].strip().upper() == "SUCCESS" for item in ret)
    ):
        # Never accept NOT_SUCCESS or an error containing SUCCESS as success.
        _raise_classified_failure(code)
    data = raw.get("data")
    if isinstance(data, str) and len(data) <= MAX_RESPONSE_BYTES:
        try:
            data = json.loads(data)
        except (TypeError, ValueError):
            data = {}
    return data if isinstance(data, dict) else {}


def _request(cookie_header: str, cookies: dict[str, str], api: str, data: dict, spm_cnt: str) -> dict:
    global _last_request_at
    check_sync_circuit()

    data_value = json.dumps(data, ensure_ascii=True, separators=(",", ":"))
    timestamp = str(int(time.time() * 1000))
    token = cookies["_m_h5_tk"].split("_", 1)[0]
    signature = hashlib.md5(f"{token}&{timestamp}&{APP_KEY}&{data_value}".encode("utf-8")).hexdigest()
    params = urllib.parse.urlencode(
        {
            "jsv": "2.7.2",
            "appKey": APP_KEY,
            "t": timestamp,
            "sign": signature,
            "v": "1.0",
            "type": "originaljson",
            "accountSite": "xianyu",
            "dataType": "json",
            "timeout": "20000",
            "api": api,
            "sessionOption": "AutoLoginOnly",
            "spm_cnt": spm_cnt,
        }
    )
    endpoint = f"{MTOP_HOST}/h5/{api}/1.0/?{params}"
    body = urllib.parse.urlencode({"data": data_value}).encode("utf-8")
    headers = browser_headers()
    headers.update(
        {
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "Cookie": cookie_header,
        }
    )
    request = urllib.request.Request(
        endpoint,
        data=body,
        method="POST",
        headers=headers,
    )

    try:
        with _request_lock:
            remaining = REQUEST_INTERVAL - (time.monotonic() - _last_request_at)
            if remaining > 0:
                time.sleep(remaining)
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                raw_body = response.read(MAX_RESPONSE_BYTES + 1)
            _last_request_at = time.monotonic()
        if len(raw_body) > MAX_RESPONSE_BYTES:
            raise ShopSyncError("platform_error", "闲鱼返回数据过大，请稍后重试")
        return json.loads(raw_body.decode("utf-8"))
    except ShopSyncError:
        raise
    except urllib.error.HTTPError as error:
        # Read only a bounded body for internal classification. Raw platform
        # text is never logged, persisted or returned to the browser.
        try:
            error_body = error.read(min(MAX_RESPONSE_BYTES, 64 * 1024)).decode("utf-8", errors="ignore")
        except (OSError, UnicodeError):
            error_body = ""
        try:
            error_payload = json.loads(error_body)
        except (TypeError, ValueError):
            body_code = _failure_code_from_text(error_body)
        else:
            body_code = _response_failure_code(error_payload) if isinstance(error_payload, dict) else "platform_error"
        if body_code != "platform_error":
            _raise_classified_failure(body_code)
        if error.code in {401, 403}:
            raise ShopSyncError("cookie_expired", "Cookie 已失效，请重新登录闲鱼后复制完整 Cookie") from None
        if error.code in {409, 412, 429}:
            raise ShopSyncError("platform_busy", "闲鱼当前请求繁忙，请稍后重试") from None
        raise ShopSyncError("network_error", "暂时无法连接闲鱼，请稍后重试") from None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, UnicodeError, OSError):
        raise ShopSyncError("network_error", "暂时无法连接闲鱼，请稍后重试") from None


def sync_shop(cookie_header: str, request_func=None, *, include_details: bool = False,
              previous_snapshot: dict | None = None, before_request=None, item_ids=None, resume_cached=False,
              progress=None, refresh_all=False) -> dict:
    if (not isinstance(include_details, bool) or not isinstance(resume_cached, bool) or not isinstance(refresh_all, bool)
            or ((resume_cached or refresh_all) and not include_details) or (progress is not None and not callable(progress))):
        raise ShopSyncError("sync_error", "商品同步选项无效")
    if not _sync_gate.acquire(blocking=False):
        raise ShopSyncError("sync_busy", "已有店铺同步正在进行，请稍后再试")
    started_at = time.monotonic()
    try:
        from catalog_sync import CATALOG_MAX_ITEMS, CATALOG_MAX_SECONDS, DETAIL_REQUEST_INTERVAL, can_resume_snapshot, merge_cached_details, refresh_catalog_details, validate_item_ids, report_progress
        item_ids = validate_item_ids(item_ids)
        if item_ids is not None and not include_details:
            raise ShopSyncError("sync_error", "指定商品刷新需要启用详情同步")
        normalized, cookies = parse_cookie_header(cookie_header)
        transport = request_func or (lambda api, data, spm: _request(normalized, cookies, api, data, spm))
        last_call_at = None
        def call(api, data, spm):
            nonlocal last_call_at
            if before_request is not None:
                before_request()
            if include_details and last_call_at is not None:
                waiting = max(0, DETAIL_REQUEST_INTERVAL - (time.monotonic() - last_call_at))
                if waiting:
                    time.sleep(waiting)
                if before_request is not None:
                    before_request()
            last_call_at = time.monotonic()
            return transport(api, data, spm)
        maximum_seconds = CATALOG_MAX_SECONDS if include_details else SYNC_MAX_SECONDS
        item_limit = CATALOG_MAX_ITEMS if include_details else MAX_ITEMS
        page_limit = math.ceil(item_limit / PAGE_SIZE) if include_details else MAX_PAGES

        reference = account_ref(cookies)
        active_only = include_details or os.environ.get("SAAS_CUSTOMER_SERVICE_ONLY") == "1"
        if active_only and previous_snapshot:
            previous_snapshot = active_catalog_snapshot(previous_snapshot)
        if resume_cached and can_resume_snapshot(previous_snapshot, reference, item_ids):
            report_progress(progress, "listing", total=0, skipped=len(previous_snapshot["products"]))
            if before_request is not None:
                before_request()
            items, receipt = refresh_catalog_details(previous_snapshot["products"], cookies["unb"], call,
                deadline=started_at + maximum_seconds, before_request=before_request, item_ids=item_ids,
                progress=progress, refresh_all=refresh_all)
            receipt["resumed_cached"] = True
            return {**previous_snapshot, "products": items, "product_count": len(items), "catalog_sync": receipt,
                    "list_synced_at": previous_snapshot.get("list_synced_at") or previous_snapshot.get("synced_at", ""),
                    "synced_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}

        if include_details:
            report_progress(progress, "listing", total=None)
        profile = _classify_response(call(PROFILE_API, {}, "a21ybx.im.0.0"))
        # Some account responses expose the authenticated numeric ID.  When
        # present, require it to agree with ``unb`` so a mixed Cookie header
        # cannot bind one seller's profile to another seller's snapshot.  A
        # response without an ID remains compatible with older mtop shapes.
        profile_id = _profile_id(profile)
        if profile_id and profile_id != cookies.get("unb", ""):
            raise ShopSyncError("cookie_invalid", "登录账号与闲鱼店铺信息不匹配，请重新登录")
        nickname = _profile_text(profile, _PROFILE_NICK_KEYS, 80) or _cookie_nickname(cookies)
        if not nickname:
            raise ShopSyncError("profile_missing", "已验证账号，但没有读取到闲鱼昵称，请稍后重试")

        synced_at = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())
        items: list[dict] = []
        seen: set[str] = set()
        page = 1
        truncated = False
        while page <= page_limit and len(items) < item_limit:
            if include_details:
                report_progress(progress, "listing", total=None, completed=page - 1, succeeded=page - 1)
            if time.monotonic() - started_at >= maximum_seconds:
                raise ShopSyncError("sync_timeout", "店铺商品较多，同步超时，请稍后重试")
            data = _classify_response(
                call(
                    PRODUCTS_API,
                    {
                        "needGroupInfo": True,
                        "pageNumber": page,
                        "userId": cookies["unb"],
                        "pageSize": PAGE_SIZE,
                    },
                    "a21ybx.item.0.0",
                )
            )
            before = len(seen)

            candidates = []
            if page == 1 and isinstance(data.get("topItem"), dict):
                candidates.append(data["topItem"])
            if isinstance(data.get("cardList"), list):
                candidates.extend(data["cardList"])
            for candidate_index, candidate in enumerate(candidates):
                card = candidate.get("cardData", candidate) if isinstance(candidate, dict) else None
                product = extract_product(card, synced_at)
                if product and product["id"] not in seen:
                    seen.add(product["id"])
                    if active_only and not is_active_product(product):
                        continue
                    items.append(product)
                    if len(items) >= item_limit:
                        for remainder in candidates[candidate_index + 1:]:
                            remainder_card = remainder.get("cardData", remainder) if isinstance(remainder, dict) else None
                            remainder_product = extract_product(remainder_card, synced_at)
                            if remainder_product and remainder_product["id"] not in seen and (not active_only or is_active_product(remainder_product)):
                                truncated = True
                                break
                        break

            next_page = data.get("nextPage")
            has_next = next_page is True or next_page == 1 or (
                isinstance(next_page, str) and next_page.strip().lower() in {"1", "true", "yes"}
            )
            if include_details:
                end = not has_next or len(seen) == before or page == page_limit or len(items) >= item_limit
                report_progress(progress, "listing", total=page if end else None, completed=page, succeeded=page)
            if not has_next or len(seen) == before:
                break
            if page == page_limit or len(items) >= item_limit:
                truncated = True
                break
            page += 1

        reference = account_ref(cookies)
        items = merge_cached_details(items, previous_snapshot, reference)
        catalog_summary = None
        if include_details:
            items, catalog_summary = refresh_catalog_details(
                items, cookies["unb"], call, deadline=started_at + maximum_seconds,
                before_request=before_request, item_ids=item_ids,
                progress=progress, refresh_all=refresh_all,
            )
        snapshot = {
            "version": 1,
            "account_ref": reference,
            "nickname": nickname,
            "products": items,
            "product_count": len(items),
            "synced_at": synced_at,
            "list_synced_at": synced_at,
            "truncated": truncated,
        }
        if catalog_summary is not None:
            snapshot["catalog_sync"] = catalog_summary
        return active_catalog_snapshot(snapshot) if active_only else snapshot
    except ShopSyncError as error:
        if not include_details or error.code != "catalog_paused":
            raise
        reference = account_ref(cookies)
        if isinstance(previous_snapshot, dict) and previous_snapshot.get("account_ref") == reference:
            snapshot = {**previous_snapshot, "products": [dict(item) for item in previous_snapshot.get("products", [])]}
        else:
            snapshot = {"version": 1, "account_ref": reference, "nickname": "", "products": [],
                        "product_count": 0, "truncated": False, "synced_at": ""}
        counts = {status: sum(item.get("detail_sync_status", "pending") == status for item in snapshot["products"])
                  for status in ("ready", "failed", "pending")}
        snapshot["catalog_sync"] = {"status": "partial", "requested": 0, "attempted": 0,
                                    "eligible": counts["pending"], **counts, "remaining_pending": counts["pending"],
                                    "skipped_ready": counts["ready"], "skipped_failed": counts["failed"],
                                    "stopped_reason": "paused", "completed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
        return snapshot
    finally:
        _sync_gate.release()
