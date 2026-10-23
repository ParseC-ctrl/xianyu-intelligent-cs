"""Explicit, serial read-only detail refresh with bounded partial receipts."""
from __future__ import annotations

import os
import json
import re
import stat
import time
import math
from datetime import datetime, timezone

from shop_sync import REQUEST_TIMEOUT, ShopSyncError, _classify_response, extract_product, extract_product_codes, extract_product_images

DETAIL_API = "mtop.taobao.idle.pc.detail"
CATALOG_MAX_ITEMS = 1000
DETAIL_BATCH_SIZE = 20
CATALOG_CONTROL_NAME = "catalog_sync_control.json"
CATALOG_MAX_SECONDS = max(60, min(int(os.environ.get("SAAS_CATALOG_SYNC_MAX_SECONDS", "900")), 1800))
DETAIL_REQUEST_INTERVAL = max(3.0, min(float(os.environ.get("SAAS_CATALOG_SYNC_REQUEST_INTERVAL_SECONDS", "3.0")), 10.0))
STOP_CODES = frozenset({"risk_control", "verification_required", "account_restricted", "cookie_expired",
                        "risk_cooldown", "platform_busy", "detail_identity_mismatch"})
BLOCKING_CODES = STOP_CODES - {"detail_identity_mismatch"}
DETAIL_CACHE_TTL_SECONDS = 7 * 86400
CATALOG_PROGRESS_NAME = "catalog_progress.json"


def report_progress(callback, phase, *, total, completed=0, succeeded=0, failed=0,
                    batch_total=None, current_item_id="", skipped=0, remaining_after_batch=0):
    if callback is not None:
        callback({"phase": phase, "total": total, "completed": completed, "succeeded": succeeded,
                  "failed": failed, "remaining": None if total is None else max(0, total - completed),
                  "batch_total": total if batch_total is None else batch_total, "batch_completed": completed,
                  "current_item_id": str(current_item_id), "skipped": skipped,
                  "remaining_after_batch": remaining_after_batch})


class CatalogProgressWriter:
    """Fenced account-private stage receipts; no product content is accepted."""
    def __init__(self, storage, user_id, account_key, identity, ensure_current, clock=None):
        self.storage, self.user_id, self.account_key = storage, user_id, account_key
        self.identity, self.ensure_current = dict(identity), ensure_current
        self.clock = clock or time.monotonic
        self.started_at, self.phase, self.phase_started, self.last = _now(), "", self.clock(), {}

    def __call__(self, value, *, status="running"):
        self.ensure_current()
        if not isinstance(value, dict) or value.get("phase") not in {"listing", "details", "images", "saving"}:
            raise ValueError("invalid catalog progress phase")
        phase = value["phase"]
        if phase != self.phase:
            self.phase, self.phase_started = phase, self.clock()
        clean = {"phase": phase}
        for key in ("total", "completed", "succeeded", "failed", "remaining", "batch_total", "batch_completed", "skipped", "remaining_after_batch"):
            count = value.get(key, 0)
            if count is None and key in {"total", "remaining", "batch_total"}:
                clean[key] = None
            elif isinstance(count, int) and not isinstance(count, bool) and 0 <= count <= 1000:
                clean[key] = count
            else:
                raise ValueError("invalid catalog progress count")
        item_id = value.get("current_item_id", "")
        clean["current_item_id"] = item_id if isinstance(item_id, str) and re.fullmatch(r"[0-9]{1,64}", item_id) else ""
        elapsed = max(0.0, self.clock() - self.phase_started)
        completed, remaining = clean["completed"], clean["remaining"]
        eta = math.ceil(elapsed / completed * remaining) if completed > 2 and remaining is not None else None
        if status not in {"running", "completed", "partial", "paused", "failed"}:
            raise ValueError("invalid catalog progress status")
        payload = {"version": 1, **self.identity, "started_at": self.started_at, "updated_at": _now(),
                   **clean, "eta_seconds": eta, "eta_confidence": "estimated" if eta is not None else "learning", "status": status}
        # Atomic account storage supplies 0600 files and refuses symlink paths.
        self.storage.write_text(self.user_id, self.account_key, CATALOG_PROGRESS_NAME,
                                json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        self.last = clean

    def finish(self, status):
        if self.last:
            self(self.last, status=status)


def pending_details(items, *, refresh_all=False, now=None):
    if not isinstance(refresh_all, bool):
        raise ShopSyncError("sync_error", "全部详情刷新选项无效")
    current = now or datetime.now(timezone.utc)
    result = []
    for item in items:
        product = dict(item)
        if product.get("detail_sync_status") == "ready":
            try:
                stamp = datetime.fromisoformat(str(product.get("detail_synced_at", "")))
                stale = stamp.tzinfo is None or (current - stamp).total_seconds() > DETAIL_CACHE_TTL_SECONDS
            except (ValueError, TypeError):
                stale = True
            if refresh_all or stale:
                product["detail_sync_status"] = "pending"
        result.append(product)
    return result


def _now():
    return datetime.now(timezone.utc).isoformat()


def validate_item_ids(item_ids):
    if item_ids is None:
        return None
    if (not isinstance(item_ids, list) or not 1 <= len(item_ids) <= DETAIL_BATCH_SIZE
            or any(not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,64}", value) for value in item_ids)
            or len(set(item_ids)) != len(item_ids)):
        raise ShopSyncError("sync_error", "指定商品必须是最多20个不重复的商品编号")
    return item_ids


def can_resume_snapshot(snapshot, reference, item_ids=None):
    if not isinstance(snapshot, dict) or snapshot.get("account_ref") != reference or not isinstance(snapshot.get("products"), list):
        return False
    if item_ids:
        return True
    try:
        stamp = datetime.fromisoformat(str(snapshot.get("list_synced_at") or snapshot.get("synced_at", "")))
        if stamp.tzinfo is None:
            return False
        age = (datetime.now(timezone.utc) - stamp).total_seconds()
        return 0 <= age <= 86400
    except (ValueError, TypeError):
        return False


def catalog_sync_paused(storage, user_id, account_key):
    """Account-private, bounded pause control; malformed/unsafe files pause."""
    descriptor = None
    try:
        path = storage._file_path(user_id, account_key, CATALOG_CONTROL_NAME)
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 4096:
            return True
        raw = os.read(descriptor, 4097)
        control = json.loads(raw)
        if (not isinstance(control, dict) or control.get("version") != 1
                or isinstance(control.get("version"), bool) or not isinstance(control.get("paused"), bool)):
            return True
        return control["paused"]
    except FileNotFoundError:
        return False
    except (OSError, ValueError, TypeError):
        return True
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _same_product(listed, cached):
    if listed.get("merchant_code") and cached.get("merchant_code"):
        if listed["merchant_code"] != cached["merchant_code"]:
            return False
    if listed.get("title") == cached.get("title"):
        return True
    fresh_codes, old_codes = set(listed.get("codes") or []), set(cached.get("codes") or [])
    return bool(fresh_codes and old_codes and fresh_codes <= old_codes)


def _listing_unchanged(listed, cached):
    if cached.get("list_fingerprint"):
        return listed.get("list_fingerprint") == cached["list_fingerprint"]
    # Migrate a verified v1 cache only if old listing provenance is present
    # and every retained reliable listing field still agrees. A current list
    # body has no old independent source to compare, so must be refreshed once.
    return ("list_merchant_code" in cached and "list_title_codes" in cached
            and not listed.get("description")
            and all(listed.get(key, "") == cached.get(key, "")
                    for key in ("title", "price", "status", "image_url", "list_merchant_code", "list_title_codes", "list_modified_at")))


def merge_cached_details(items, previous_snapshot, account_reference):
    """Reuse only verified detail prose for the same owner and product identity."""
    if not isinstance(previous_snapshot, dict) or previous_snapshot.get("account_ref") != account_reference:
        return [dict(item) for item in items]
    previous = {str(item.get("id")): item for item in previous_snapshot.get("products", []) if isinstance(item, dict)}
    result = []
    for listed in items:
        product, old = dict(listed), previous.get(str(listed.get("id")))
        if listed.get("description_source") == "detail" and listed.get("detail_sync_status") == "ready":
            result.append(product)
            continue
        valid_old = (old and old.get("description_source") == "detail" and old.get("detail_synced_at")
                     and old.get("detail_sync_status") in {"ready", "failed", "pending"}
                     and old.get("description") and _same_product(listed, old))
        if valid_old:
            unchanged = _listing_unchanged(listed, old)
            for key in ("description", "description_source", "detail_synced_at", "detail_sync_status", "detail_error_code", "image_urls", "sku_check_pending"):
                if key in old:
                    product[key] = old[key]
            if unchanged and "sku_check" in old:
                product["sku_check"] = old["sku_check"]
            else:
                product.pop("sku_check", None)
            product["detail_cached"] = True
            if not product.get("merchant_code") and old.get("merchant_code"):
                product["merchant_code"] = old["merchant_code"]
                product["merchant_code_source"] = "cached"
            if unchanged and old.get("codes_source") == "sku_check":
                product["codes"] = list(old.get("codes") or [])
                product["codes_source"] = "sku_check"
            elif not product.get("codes") and old.get("codes") and old.get("codes_source") != "sku_check":
                product["codes"] = list(old["codes"])
                if old.get("codes_source"):
                    product["codes_source"] = old["codes_source"]
            if not unchanged or "image_urls" not in old:
                product["detail_sync_status"] = "pending"
                product.pop("detail_error_code", None)
        elif old and _listing_unchanged(listed, old) and old.get("detail_sync_status") == "failed":
            product["detail_sync_status"] = "failed"
            product["detail_error_code"] = old.get("detail_error_code", "detail_missing")
        result.append(product)
    return pending_details(result)


def _detail_record(data, item_id, seller_id, stamp, listed_title="", listed_merchant="", listed_title_codes=None):
    node = next((data[key] for key in ("itemDO", "itemInfo", "item") if isinstance(data.get(key), dict)), data)
    received_id = str(node.get("itemId") or node.get("id") or "")
    owner = str(node.get("sellerId") or node.get("userId") or node.get("ownerId") or "")
    if (received_id and received_id != item_id) or (owner and owner != seller_id):
        raise ShopSyncError("detail_identity_mismatch", "商品详情身份不一致，本次详情同步已停止")
    description = node.get("desc") or node.get("description") or node.get("itemDesc")
    if not isinstance(description, str) or not description.strip():
        raise ShopSyncError("detail_missing", "商品详情没有返回正文")
    record = extract_product({**node, "id": item_id}, stamp, description_limit=20000)
    if not record or not record.get("description"):
        raise ShopSyncError("detail_missing", "商品详情没有返回正文")
    record.update(description_source="detail", detail_sync_status="ready", detail_synced_at=stamp)
    images = list(dict.fromkeys(extract_product_images(node) + extract_product_images(data)))
    record["image_urls"] = images if len(images) <= 20 else images[:19] + [images[-1]]
    # Build the identity from current list/title provenance and this response's
    # new body. Cached-body-derived codes and merchants are never carried into
    # a successful refresh merely because the new response lacks that field.
    merchant = listed_merchant or record.get("merchant_code", "")
    record["merchant_code"] = merchant
    record["merchant_code_source"] = "list" if listed_merchant else "detail" if merchant else ""
    record["codes"] = list(dict.fromkeys(
        (listed_title_codes or []) + extract_product_codes(listed_title, description, merchant)))[:30]
    record["codes_source"] = "detail"
    return record


def refresh_catalog_details(items, seller_id, call, *, deadline, before_request=None,
                            item_ids=None, progress=None, refresh_all=False, clock=time.monotonic, sleeper=time.sleep):
    item_ids = validate_item_ids(item_ids)
    products = pending_details(items, refresh_all=refresh_all)
    eligible = [item for item in products if item.get("detail_sync_status", "pending") == "pending"]
    selected = ([item for item in products if item["id"] in item_ids] if item_ids is not None else eligible[:DETAIL_BATCH_SIZE])
    skipped_ready = sum(item.get("detail_sync_status") == "ready" for item in products if item not in selected)
    skipped_failed = sum(item.get("detail_sync_status") == "failed" for item in products if item not in selected)
    for item in selected:
        item["detail_sync_status"] = "pending"
    attempted, stopped_reason, last_attempt, service_failures = 0, "", None, 0
    def update(current=""):
        succeeded = sum(product.get("detail_sync_status") == "ready" for product in selected)
        failed = sum(product.get("detail_sync_status") == "failed" for product in selected)
        report_progress(progress, "details", total=len(selected), completed=succeeded + failed,
                        succeeded=succeeded, failed=failed, current_item_id=current,
                        skipped=skipped_ready + skipped_failed, remaining_after_batch=max(0, len(eligible) - len(selected)))
    update()
    for product in selected:
        update(str(product["id"]))
        item_attempted = False
        waiting = max(0.0, DETAIL_REQUEST_INTERVAL - (clock() - last_attempt)) if last_attempt is not None else 0.0
        if clock() + waiting + REQUEST_TIMEOUT >= deadline:
            stopped_reason = "budget_exhausted"
            break
        try:
            if before_request:
                before_request()
            if waiting:
                sleeper(waiting)
            if before_request:
                before_request()
            attempted += 1
            item_attempted = True
            last_attempt = clock()
            raw = call(DETAIL_API, {"itemId": str(product["id"])}, "a21ybx.item.0.0")
            detail = _detail_record(_classify_response(raw), str(product["id"]), str(seller_id), _now(),
                                    product.get("title", ""), product.get("list_merchant_code", ""),
                                    product.get("list_title_codes", []))
            # Never replace fresh list ownership/status/price with absent detail
            # fields. Only verified descriptive facts are merged.
            for field in ("title", "description", "image_url"):
                value = detail.get(field)
                if value and not (field == "title" and value == "未命名商品"):
                    product[field] = value
            product["codes"] = detail["codes"]
            product["codes_source"] = detail["codes_source"]
            product["image_urls"] = detail["image_urls"]
            product.pop("sku_check", None)
            if detail["merchant_code"]:
                product["merchant_code"] = detail["merchant_code"]
                product["merchant_code_source"] = detail["merchant_code_source"]
            else:
                product.pop("merchant_code", None)
                product.pop("merchant_code_source", None)
            product.update(description_source="detail", detail_sync_status="ready", detail_synced_at=detail["detail_synced_at"], detail_cached=False)
            product.pop("detail_error_code", None)
            service_failures = 0
        except ShopSyncError as error:
            if error.code == "catalog_paused":
                if item_attempted:
                    attempted -= 1
                stopped_reason = "paused"
                break
            product["detail_sync_status"] = "failed"
            product["detail_error_code"] = error.code
            if error.code in STOP_CODES:
                stopped_reason = error.code
                break
            service_failures = service_failures + 1 if error.code in {"network_error", "platform_error"} else 0
            if service_failures >= 3:
                stopped_reason = error.code
                break
            # No retry of this item. A network/malformed detail failure leaves
            # its previously verified cached facts untouched.
        finally:
            update(str(product["id"]))
    counts = {status: sum(item.get("detail_sync_status", "pending") == status for item in products) for status in ("ready", "failed", "pending")}
    if not stopped_reason and item_ids is None and counts["pending"]:
        stopped_reason = "batch_limit"
    summary = {"status": "ready" if not counts["failed"] and not counts["pending"] else "partial",
               "requested": len(selected), "eligible": len(eligible), "attempted": attempted, **counts,
               "skipped_ready": skipped_ready, "skipped_failed": skipped_failed, "remaining_pending": counts["pending"],
               "not_found_ids": [value for value in item_ids or [] if not any(item["id"] == value for item in products)],
               "stopped_reason": stopped_reason, "completed_at": _now()}
    return products, summary
