"""Read a product's last photo once and reconcile exact catalogue identifiers."""
from __future__ import annotations

import hashlib
import json
import re
import time
import unicodedata
from urllib.parse import urlsplit, urlunsplit

from material_knowledge import _found_codes


def safe_image_url(value):
    if not isinstance(value, str) or len(value) > 2048:
        return ""
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower()
        if (parsed.scheme not in {"http", "https"} or parsed.username or parsed.password or parsed.port not in (None, 80 if parsed.scheme == "http" else 443)
                or any(c.isspace() for c in value)
                or not any(host == d or host.endswith("." + d) for d in ("alicdn.com", "taobaocdn.com"))):
            return ""
        return urlunsplit(("https", host, parsed.path, parsed.query, parsed.fragment))
    except ValueError:
        return ""


def normalize_code(value):
    if not isinstance(value, str):
        return ""
    code = unicodedata.normalize("NFKC", value).strip().upper()
    return code if re.fullmatch(r"[A-Z0-9][A-Z0-9_.-]{2,79}", code) else ""


def description_codes(product, known):
    def normalized_text(value):
        text = unicodedata.normalize("NFKC", str(value or "")).upper()
        return re.sub(r"\s*-\s*", "-", re.sub(r"[‐‑‒–—―−﹣－]", "-", text))

    def description_identifier(value):
        code = normalize_code(value)
        # With only one suffix it might be a colour/model component rather
        # than a shoe size; require a complete style-colour-size structure.
        if code not in known and code.count("-") < 2:
            return code
        return normalize_visual_code(code, known)

    def known_tokens(text):
        # Match the complete token before considering a trailing shoe size.
        # This avoids treating an unknown longer identifier as a known prefix.
        tokens = re.findall(
            r"(?<![A-Z0-9_.-])((?:[A-Z]{1,8}[0-9][A-Z0-9]{2,24}|[0-9]{4,24})"
            r"(?:[-.][A-Z0-9]{1,10}){0,3})(?![A-Z0-9]|[.-][A-Z0-9])", text)
        return {code for token in tokens if (code := description_identifier(token)) in known}

    text = normalized_text(product.get("description"))
    # An explicitly labelled unknown code still wins over an image code. Do
    # not silently replace an unknown description SKU with a known visual one.
    labelled = re.findall(r"(?:货号|款号|商品编码|SKU)\s*[:：#]?\s*([A-Za-z0-9][A-Za-z0-9_.-]{2,79})", text, re.I)
    hits = {description_identifier(c) for c in labelled} - {""}
    hits.update(_found_codes(text, known))
    hits.update(known_tokens(text))
    if not hits:
        title = normalized_text(product.get("title"))
        hits.update(_found_codes(title, known))
        hits.update(known_tokens(title))
    return sorted(hits)


VISION_READER_VERSION = 2


def needs_sku_check(product, previous=None):
    return (isinstance(product, dict) and "image_urls" in product and product.get("detail_sync_status") == "ready"
            and bool(product.get("detail_synced_at")) and (product.get("sku_check_pending")
            or not isinstance(product.get("sku_check"), dict)
            or product["sku_check"].get("reader_version") != VISION_READER_VERSION
            or product.get("detail_synced_at") != (previous or {}).get("detail_synced_at")))


def normalize_visual_code(value, known):
    if not isinstance(value, str):
        return ""
    clean = unicodedata.normalize("NFKC", value).upper()
    clean = re.sub(r"[‐‑‒–—−]", "-", clean)
    clean = re.sub(r"\s+", "", clean)
    code = normalize_code(clean)
    if code not in known:
        base, separator, size = code.rpartition("-")
        if separator and size.isdigit() and 35 <= int(size) <= 50 and base in known:
            return base
    return code


def reconcile_sku(product, known, read_image, previous=None):
    desc = description_codes(product, known)
    images = product.get("image_urls") or []
    last_image = safe_image_url(images[-1]) if isinstance(images, list) and images else ""
    evidence = []
    visual, error, visual_status = [], "image_unsafe" if images and not last_image else "", "none"
    image_hash = hashlib.sha256(last_image.encode()).hexdigest() if last_image else ""
    old = (previous or {}).get("sku_check") or {}
    if last_image:
        try:
            if old.get("image_fingerprint") == image_hash and old.get("vision_status") == "clear" and old.get("reader_version") == VISION_READER_VERSION:
                visual = [normalize_code(c) for c in old.get("image_codes", [])]
                visual_status = "clear"
                evidence = old.get("image_evidence", [])
            else:
                result = read_image(last_image)
                if (not isinstance(result, dict) or result.get("status") not in {"clear", "none", "uncertain"}
                        or not isinstance(result.get("codes"), list) or len(result["codes"]) > 5):
                    raise ValueError("invalid vision result")
                raw_evidence = result.get("evidence", [])
                if isinstance(raw_evidence, list):
                    evidence = [line.strip()[:200] for line in raw_evidence[:5] if isinstance(line, str)]
                visual_status = result["status"]
                visual = [normalize_visual_code(c, known) for c in result["codes"]]
                if any(not c for c in visual) or (visual_status != "clear" and visual):
                    raise ValueError("invalid vision identifiers")
            visual = sorted(set(visual))
        except Exception:
            visual, visual_status, error = [], "unavailable", "vision_unavailable"
    selected = ""
    if len(desc) > 1:
        status = "ambiguous"
    elif desc:
        selected = desc[0]
        status = "matched" if visual == desc else "conflict" if visual else "description_only"
    elif len(visual) == 1:
        selected, status = visual[0], "image_only"
    elif len(visual) > 1 or visual_status == "uncertain":
        status = "ambiguous"
    else:
        status = "unavailable" if error else "not_found"
    check = {"version": 1, "reader_version": VISION_READER_VERSION, "status": status, "description_code": desc[0] if len(desc) == 1 else "",
             "description_codes": desc, "image_code": visual[0] if len(visual) == 1 else "",
             "image_codes": visual, "image_evidence": evidence, "selected_code": selected, "vision_status": visual_status,
             "image_fingerprint": image_hash, "error_code": error,
             "source": "description" if selected and desc else "image" if selected else ""}
    return {**product, "sku_check": check, "codes": [selected] if selected else [], "codes_source": "sku_check"}


def enrich_snapshot_skus(snapshot, previous, service, scope, before_request, progress=None):
    from catalog_sync import report_progress
    old_products = {str(p.get("id")): p for p in (previous or {}).get("products", []) if isinstance(p, dict)}
    fresh_all = [p for p in snapshot.get("products", [])
                 if needs_sku_check(p, old_products.get(str(p.get("id"))))]
    fresh = fresh_all[:20]
    succeeded = failed = 0
    def update(current=""):
        report_progress(progress, "images", total=len(fresh), completed=succeeded + failed,
                        succeeded=succeeded, failed=failed, current_item_id=current,
                        skipped=len(snapshot.get("products", [])) - len(fresh_all),
                        remaining_after_batch=max(0, len(fresh_all) - len(fresh)) + sum(
                            p.get("detail_sync_status") == "pending" for p in snapshot.get("products", []) if isinstance(p, dict)))
    update()
    if not fresh:
        return snapshot
    store = service.material_store(*scope)
    try:
        records = store._records()
    except Exception:
        records = []
    known = {r["sku"] for r in records} | {r["style_code"] for r in records if r["style_code"]}
    replacements = {}
    from shop_sync import ShopSyncError
    halt_reason = ""
    deadline = time.monotonic() + 300
    provider_failed = False
    def read_image(url):
        nonlocal provider_failed
        if provider_failed:
            raise ValueError("vision temporarily unavailable")
        try:
            return service.read_catalog_image(scope, url)
        except Exception as error:
            # A bad/oversized individual photo must not suppress valid photos
            # on later products. Only connection/provider-wide errors trip this.
            code = str(getattr(error, "code", ""))
            provider_failed = code in {"authentication_failed", "rate_limited", "model_not_found",
                "vision_provider_unsupported", "credential_unavailable", "invalid_payload"} or code.startswith("connection_")
            raise
    for product in fresh:
        update(str(product["id"]))
        try:
            if time.monotonic() >= deadline:
                halt_reason = "image_budget_exhausted"
                break
            before_request()
            value = reconcile_sku(product, known, read_image,
                                  old_products.get(str(product["id"])))
            value.pop("sku_check_pending", None)
            replacements[str(product["id"])] = value
            if value.get("sku_check", {}).get("error_code"):
                failed += 1
            else:
                succeeded += 1
            update(str(product["id"]))
            before_request()
        except ShopSyncError as error:
            if error.code != "catalog_paused":
                raise
            halt_reason = "paused"
            break
    for product in fresh_all[20:]:
        replacements[str(product["id"])] = {**product, "sku_check_pending": True}
    if fresh_all[20:] and not halt_reason:
        halt_reason = "image_batch_limit"
    if halt_reason:
        for product in fresh:
            if str(product["id"]) not in replacements:
                replacements[str(product["id"])] = {**product, "sku_check_pending": True}
        receipt = dict(snapshot.get("catalog_sync") or {})
        receipt.update(status="partial", stopped_reason=halt_reason)
        receipt["remaining_pending"] = receipt.get("remaining_pending", 0) + sum(
            bool(p.get("sku_check_pending")) for p in replacements.values())
        snapshot = {**snapshot, "catalog_sync": receipt}
    products = [replacements.get(str(p.get("id")), p) for p in snapshot.get("products", [])]
    receipt = dict(snapshot.get("catalog_sync") or {})
    receipt["remaining_pending"] = sum(p.get("detail_sync_status", "pending") == "pending" or bool(p.get("sku_check_pending")) for p in products)
    return {**snapshot, "catalog_sync": receipt, "products": products}
