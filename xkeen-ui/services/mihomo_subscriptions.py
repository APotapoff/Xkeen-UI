"""Managed Mihomo subscriptions produced from Xray-JSON sources.

Mihomo cannot consume XKeen/Xray JSON subscriptions directly as
``proxy-provider`` entries, so the generator converts them to static proxy YAML.
This service keeps the original URL next to the generated YAML block and can
refresh that block later from the saved generator state.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import threading
import time
from typing import Any, Callable, Dict, List, Tuple
from urllib.parse import urlparse

from mihomo_config_generator import build_full_config
from services.io.atomic import _atomic_write_json, _atomic_write_text
from services.mihomo_xray_json import convert_subscription_text
from services.url_policy import env_flag
from services.xray_subscriptions import fetch_subscription_body
from utils.fs import load_text


STATE_VERSION = 1
STATE_FILENAME = "mihomo_subscriptions.json"

DEFAULT_INTERVAL_HOURS = 24
MIN_INTERVAL_HOURS = 1
MAX_INTERVAL_HOURS = 168

_STATE_LOCK = threading.RLock()
_SCHEDULER_LOCK = threading.Lock()
_SCHEDULER_STARTED = False

RestartCallback = Callable[..., Any]
SaveCallback = Callable[[str], Any]


def subscription_state_path(ui_state_dir: str) -> str:
    return os.path.join(str(ui_state_dir or "/opt/etc/xkeen-ui"), STATE_FILENAME)


def _now() -> float:
    return time.time()


def _hash_text(text: Any) -> str:
    normalised = str(text or "").replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")
    return hashlib.sha256(normalised.encode("utf-8", errors="ignore")).hexdigest()


def _read_json_file(path: str, default: Any) -> Any:
    try:
        text = load_text(path, default=None)
        if text is None:
            return default
        return json.loads(text)
    except Exception:
        return default


def _write_state(ui_state_dir: str, state: Dict[str, Any]) -> None:
    path = subscription_state_path(ui_state_dir)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    _atomic_write_json(path, state)


def _clean_id(value: Any) -> str:
    raw = str(value or "").strip().lower()
    raw = re.sub(r"[^a-z0-9_.-]+", "-", raw)
    raw = re.sub(r"-{2,}", "-", raw).strip("-._")
    if not raw:
        raw = "mihomo-sub"
    if raw[0].isdigit():
        raw = "sub-" + raw
    return raw[:60].strip("-._") or "mihomo-sub"


def _clamp_interval(value: Any) -> int:
    try:
        hours = int(float(str(value).strip()))
    except Exception:
        hours = DEFAULT_INTERVAL_HOURS
    return max(MIN_INTERVAL_HOURS, min(MAX_INTERVAL_HOURS, hours))


def _clean_string_list(value: Any) -> List[str]:
    if isinstance(value, (list, tuple, set)):
        raw = value
    else:
        raw = re.split(r"[,;]+", str(value or "").strip()) if str(value or "").strip() else []
    out: List[str] = []
    seen: set[str] = set()
    for item in raw:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def _derive_tag_from_url(url: str) -> str:
    try:
        host = urlparse(str(url or "")).hostname or ""
    except Exception:
        host = ""
    return "xray-sub:" + host if host else "xray-sub"


def _entry_id_from_url(url: str, tag: str) -> str:
    digest = hashlib.sha1(str(url or "").encode("utf-8", errors="ignore")).hexdigest()[:10]
    base = _clean_id(str(tag or "").replace("xray-sub:", "xray-"))
    return _clean_id(f"{base}-{digest}")


def _normalise_saved_state(raw: Any) -> Dict[str, Any]:
    state = raw if isinstance(raw, dict) else {}
    subs_raw = state.get("subscriptions") if isinstance(state.get("subscriptions"), list) else []
    subs: List[Dict[str, Any]] = []
    for item in subs_raw:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        sub_id = _clean_id(item.get("id") or _entry_id_from_url(url, item.get("tag") or "xray-sub"))
        entry = dict(item)
        entry["id"] = sub_id
        entry["url"] = url
        entry["tag"] = str(item.get("tag") or _derive_tag_from_url(url)).strip() or "xray-sub"
        entry["enabled"] = bool(item.get("enabled", True))
        entry["interval_hours"] = _clamp_interval(item.get("interval_hours", item.get("intervalHours")))
        if not entry["enabled"]:
            entry["next_update_ts"] = None
        elif entry.get("next_update_ts") in (None, ""):
            entry["next_update_ts"] = _now() + entry["interval_hours"] * 3600
        subs.append(entry)

    return {
        "version": STATE_VERSION,
        "subscriptions": subs,
        "generator_state": copy.deepcopy(state.get("generator_state") or {}),
        "last_config_hash": str(state.get("last_config_hash") or ""),
        "last_synced_ts": float(state.get("last_synced_ts") or 0),
    }


def load_subscription_state(ui_state_dir: str) -> Dict[str, Any]:
    return _normalise_saved_state(_read_json_file(subscription_state_path(ui_state_dir), {}))


def list_subscriptions(ui_state_dir: str) -> List[Dict[str, Any]]:
    state = load_subscription_state(ui_state_dir)
    return copy.deepcopy(state.get("subscriptions") or [])


def update_subscription_settings(
    ui_state_dir: str,
    sub_id: str,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    """Update persisted settings for one managed Mihomo subscription."""
    data = payload if isinstance(payload, dict) else {}
    with _STATE_LOCK:
        state = load_subscription_state(ui_state_dir)
        sub_idx, sub = _find_subscription(state, sub_id)
        if sub_idx < 0 or sub is None:
            raise KeyError("subscription not found")

        now_ts = _now()
        entry = dict(sub)
        old_interval = _clamp_interval(entry.get("interval_hours"))
        if "interval_hours" in data or "intervalHours" in data or "interval" in data:
            interval = _clamp_interval(
                data.get("interval_hours", data.get("intervalHours", data.get("interval")))
            )
        else:
            interval = old_interval

        if "enabled" in data:
            enabled = bool(data.get("enabled"))
        else:
            enabled = bool(entry.get("enabled", True))

        schedule_changed = interval != old_interval or enabled != bool(entry.get("enabled", True))
        entry["interval_hours"] = interval
        entry["enabled"] = enabled
        entry["settings_updated_ts"] = now_ts
        if not enabled:
            entry["next_update_ts"] = None
        elif schedule_changed or entry.get("next_update_ts") in (None, ""):
            entry["next_update_ts"] = now_ts + interval * 3600

        generator_state = state.get("generator_state")
        if isinstance(generator_state, dict):
            proxy_index = _find_proxy_index_for_subscription(generator_state, entry)
            proxies = generator_state.get("proxies")
            if proxy_index >= 0 and isinstance(proxies, list) and proxy_index < len(proxies):
                proxy = proxies[proxy_index]
                if isinstance(proxy, dict):
                    meta = _extract_meta(proxy) or {}
                    meta.update(
                        {
                            "id": entry.get("id"),
                            "url": entry.get("url"),
                            "tag": entry.get("tag") or _derive_tag_from_url(entry.get("url") or ""),
                            "enabled": enabled,
                            "interval_hours": interval,
                            "proxy_index": proxy_index,
                        }
                    )
                    proxy["xray_json_subscription"] = meta
                    proxy.pop("xrayJsonSubscription", None)
                    proxy.pop("xraySubscription", None)
                    entry["proxy_index"] = proxy_index

        state["subscriptions"][sub_idx] = entry
        _write_state(ui_state_dir, state)
        return copy.deepcopy(entry)


def _extract_meta(proxy: Dict[str, Any]) -> Dict[str, Any] | None:
    for key in ("xray_json_subscription", "xrayJsonSubscription", "xraySubscription"):
        value = proxy.get(key)
        if isinstance(value, dict):
            return value
    return None


def _normalise_proxy_meta(
    proxy: Dict[str, Any],
    *,
    index: int,
    previous: Dict[str, Any] | None = None,
    used_ids: set[str] | None = None,
) -> Dict[str, Any] | None:
    meta = _extract_meta(proxy)
    if not isinstance(meta, dict):
        return None

    url = str(meta.get("url") or meta.get("source_url") or meta.get("sourceUrl") or "").strip()
    if not url:
        return None

    tags = _clean_string_list(proxy.get("tags"))
    tag = str(meta.get("tag") or "").strip()
    if not tag:
        tag = next((t for t in tags if t.startswith("xray-sub:")), "") or _derive_tag_from_url(url)

    wanted_id = _clean_id(meta.get("id") or (previous or {}).get("id") or _entry_id_from_url(url, tag))
    used_ids = used_ids if used_ids is not None else set()
    sub_id = wanted_id
    suffix = 2
    while sub_id in used_ids:
        sub_id = _clean_id(f"{wanted_id}-{suffix}")
        suffix += 1
    used_ids.add(sub_id)

    interval = _clamp_interval(
        meta.get("interval_hours", meta.get("intervalHours", (previous or {}).get("interval_hours")))
    )

    return {
        "id": sub_id,
        "url": url,
        "tag": tag,
        "enabled": bool(meta.get("enabled", (previous or {}).get("enabled", True))),
        "interval_hours": interval,
        "proxy_index": index,
    }


def _entry_from_proxy_meta(
    proxy: Dict[str, Any],
    meta: Dict[str, Any],
    *,
    previous: Dict[str, Any] | None = None,
    now_ts: float,
) -> Dict[str, Any]:
    prev = dict(previous or {})
    changed_schedule = (
        str(prev.get("url") or "") != str(meta.get("url") or "")
        or int(prev.get("interval_hours") or 0) != int(meta.get("interval_hours") or 0)
        or bool(prev.get("enabled", True)) != bool(meta.get("enabled", True))
    )

    entry = {
        **prev,
        "id": meta["id"],
        "url": meta["url"],
        "tag": meta["tag"],
        "enabled": bool(meta.get("enabled", True)),
        "interval_hours": int(meta.get("interval_hours") or DEFAULT_INTERVAL_HOURS),
        "proxy_index": int(meta.get("proxy_index") or 0),
        "groups": _clean_string_list(proxy.get("groups")),
        "updated_from_generator_ts": now_ts,
    }
    entry.setdefault("created_ts", now_ts)

    if not entry["enabled"]:
        entry["next_update_ts"] = None
    elif changed_schedule or entry.get("next_update_ts") in (None, ""):
        entry["next_update_ts"] = now_ts + entry["interval_hours"] * 3600

    return entry


def sync_from_generator_state(
    ui_state_dir: str,
    generator_state: Dict[str, Any],
    *,
    config_text: str | None = None,
) -> Dict[str, Any]:
    """Persist the generator state and managed Xray-JSON subscription metadata."""
    with _STATE_LOCK:
        previous_state = load_subscription_state(ui_state_dir)
        previous_by_id = {
            str(item.get("id") or ""): item
            for item in previous_state.get("subscriptions") or []
            if isinstance(item, dict)
        }
        now_ts = _now()
        state_copy = copy.deepcopy(generator_state if isinstance(generator_state, dict) else {})
        proxies = state_copy.get("proxies")
        if not isinstance(proxies, list):
            proxies = []
            state_copy["proxies"] = proxies

        used_ids: set[str] = set()
        entries: List[Dict[str, Any]] = []
        for idx, proxy in enumerate(proxies):
            if not isinstance(proxy, dict):
                continue
            previous = None
            existing_meta = _extract_meta(proxy)
            if isinstance(existing_meta, dict) and existing_meta.get("id"):
                previous = previous_by_id.get(_clean_id(existing_meta.get("id")))

            meta = _normalise_proxy_meta(proxy, index=idx, previous=previous, used_ids=used_ids)
            if not meta:
                continue
            if previous is None:
                previous = previous_by_id.get(meta["id"])

            tags = _clean_string_list(proxy.get("tags"))
            if meta["tag"] and meta["tag"] not in tags:
                tags.append(meta["tag"])
                proxy["tags"] = tags

            proxy["xray_json_subscription"] = dict(meta)
            proxy.pop("xrayJsonSubscription", None)
            proxy.pop("xraySubscription", None)
            entries.append(_entry_from_proxy_meta(proxy, meta, previous=previous, now_ts=now_ts))

        out_state = {
            "version": STATE_VERSION,
            "subscriptions": entries,
            "generator_state": state_copy,
            "last_config_hash": previous_state.get("last_config_hash") or "",
            "last_synced_ts": now_ts,
        }
        if config_text is not None:
            out_state["last_config_hash"] = _hash_text(config_text)

        _write_state(ui_state_dir, out_state)
        return copy.deepcopy(out_state)


def _find_subscription(state: Dict[str, Any], sub_id: str) -> Tuple[int, Dict[str, Any] | None]:
    wanted = _clean_id(sub_id)
    for idx, item in enumerate(state.get("subscriptions") or []):
        if isinstance(item, dict) and _clean_id(item.get("id")) == wanted:
            return idx, item
    return -1, None


def _find_proxy_index_for_subscription(generator_state: Dict[str, Any], sub: Dict[str, Any]) -> int:
    proxies = generator_state.get("proxies")
    if not isinstance(proxies, list):
        return -1

    candidates: List[int] = []
    try:
        proxy_index = int(sub.get("proxy_index"))
        candidates.append(proxy_index)
    except Exception:
        pass
    candidates.extend(range(len(proxies)))

    seen: set[int] = set()
    for idx in candidates:
        if idx in seen or idx < 0 or idx >= len(proxies):
            continue
        seen.add(idx)
        proxy = proxies[idx]
        if not isinstance(proxy, dict):
            continue
        meta = _extract_meta(proxy) or {}
        if _clean_id(meta.get("id")) == _clean_id(sub.get("id")):
            return idx
        if str(meta.get("url") or "").strip() and str(meta.get("url") or "").strip() == str(sub.get("url") or "").strip():
            return idx
    return -1


def _extract_proxy_names_from_yaml(yaml_text: Any) -> List[str]:
    names: List[str] = []
    for match in re.finditer(r"(?m)^\s*-\s*name\s*:\s*([^\n#]+)", str(yaml_text or "")):
        value = match.group(1).strip().strip('"').strip("'")
        if value:
            names.append(value)
    return names


def _existing_names_for_refresh(generator_state: Dict[str, Any], *, skip_index: int) -> List[str]:
    proxies = generator_state.get("proxies")
    if not isinstance(proxies, list):
        return []
    names: List[str] = []
    seen: set[str] = set()
    for idx, proxy in enumerate(proxies):
        if idx == skip_index or not isinstance(proxy, dict):
            continue
        if str(proxy.get("name") or "").strip():
            names.append(str(proxy.get("name")).strip())
        if str(proxy.get("kind") or "").lower() == "yaml":
            names.extend(_extract_proxy_names_from_yaml(proxy.get("yaml")))
    out: List[str] = []
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


def _format_proxy_yaml_blocks(proxies: Any) -> str:
    blocks: List[str] = []
    for proxy in proxies or []:
        text = str(getattr(proxy, "yaml", "") or "").strip()
        if text:
            blocks.append(text)
    return "\n\n".join(blocks)


def _schedule_next(entry: Dict[str, Any], now_ts: float) -> None:
    if not bool(entry.get("enabled", True)):
        entry["next_update_ts"] = None
        return
    entry["next_update_ts"] = now_ts + _clamp_interval(entry.get("interval_hours")) * 3600


def _mark_failed(
    ui_state_dir: str,
    state: Dict[str, Any],
    sub_idx: int,
    entry: Dict[str, Any],
    error: str,
    *,
    now_ts: float,
) -> Dict[str, Any]:
    entry = dict(entry)
    entry["last_ok"] = False
    entry["last_error"] = str(error or "refresh_failed")
    entry["last_update_ts"] = now_ts
    _schedule_next(entry, now_ts)
    state["subscriptions"][sub_idx] = entry
    _write_state(ui_state_dir, state)
    return {
        "ok": False,
        "id": entry.get("id"),
        "changed": False,
        "restarted": False,
        "error": entry["last_error"],
        "next_update_ts": entry.get("next_update_ts"),
    }


def refresh_subscription(
    ui_state_dir: str,
    sub_id: str,
    *,
    mihomo_config_file: str,
    restart_xkeen: RestartCallback | None = None,
    restart: bool = True,
    force: bool = False,
    save_callback: SaveCallback | None = None,
) -> Dict[str, Any]:
    now_ts = _now()
    with _STATE_LOCK:
        state = load_subscription_state(ui_state_dir)
        sub_idx, sub = _find_subscription(state, sub_id)
        if sub_idx < 0 or sub is None:
            raise KeyError("subscription not found")
        sub = dict(sub)

        active_text = load_text(mihomo_config_file, default="") or ""
        expected_hash = str(state.get("last_config_hash") or "")
        if expected_hash and _hash_text(active_text) != expected_hash and not force:
            return _mark_failed(
                ui_state_dir,
                state,
                sub_idx,
                sub,
                "active_config_changed",
                now_ts=now_ts,
            )

        generator_state = copy.deepcopy(state.get("generator_state") or {})
        proxy_index = _find_proxy_index_for_subscription(generator_state, sub)
        if proxy_index < 0:
            return _mark_failed(
                ui_state_dir,
                state,
                sub_idx,
                sub,
                "managed_proxy_not_found",
                now_ts=now_ts,
            )

        try:
            body, _headers = fetch_subscription_body(str(sub.get("url") or ""))
            proxies, skipped = convert_subscription_text(
                body,
                existing_names=_existing_names_for_refresh(generator_state, skip_index=proxy_index),
            )
            if not proxies:
                raise RuntimeError("no_supported_proxies")
            proxy_yaml = _format_proxy_yaml_blocks(proxies)
            if not proxy_yaml.strip():
                raise RuntimeError("empty_proxy_yaml")

            proxies_list = generator_state.get("proxies")
            if not isinstance(proxies_list, list):
                raise RuntimeError("generator_state_invalid")
            proxy_item = proxies_list[proxy_index]
            if not isinstance(proxy_item, dict):
                raise RuntimeError("managed_proxy_invalid")

            proxy_item["kind"] = "yaml"
            proxy_item["yaml"] = proxy_yaml
            tags = _clean_string_list(proxy_item.get("tags"))
            tag = str(sub.get("tag") or _derive_tag_from_url(sub.get("url") or "")).strip() or "xray-sub"
            if tag not in tags:
                tags.append(tag)
            proxy_item["tags"] = tags
            proxy_item["xray_json_subscription"] = {
                "id": sub.get("id"),
                "url": sub.get("url"),
                "tag": tag,
                "enabled": bool(sub.get("enabled", True)),
                "interval_hours": _clamp_interval(sub.get("interval_hours")),
                "proxy_index": proxy_index,
            }

            cfg = build_full_config(generator_state)
            cfg_to_save = cfg.rstrip("\n")
            changed = _hash_text(cfg_to_save) != _hash_text(active_text)
            if changed:
                if save_callback is not None:
                    save_callback(cfg_to_save)
                else:
                    _atomic_write_text(mihomo_config_file, cfg_to_save + "\n")

                if restart and restart_xkeen is not None:
                    try:
                        restart_xkeen(source="mihomo-subscription-refresh")
                    except TypeError:
                        restart_xkeen("mihomo-subscription-refresh")

            final_hash = _hash_text(cfg_to_save if changed else active_text)
            sub["proxy_index"] = proxy_index
            sub["last_ok"] = True
            sub["last_error"] = ""
            sub["last_update_ts"] = now_ts
            sub["last_count"] = len(proxies)
            sub["last_skipped_count"] = len(skipped)
            sub["last_hash"] = _hash_text(proxy_yaml)
            sub["last_changed"] = bool(changed)
            _schedule_next(sub, now_ts)

            state["generator_state"] = generator_state
            state["last_config_hash"] = final_hash
            state["last_synced_ts"] = now_ts
            state["subscriptions"][sub_idx] = sub
            _write_state(ui_state_dir, state)

            return {
                "ok": True,
                "id": sub.get("id"),
                "changed": bool(changed),
                "restarted": bool(changed and restart and restart_xkeen is not None),
                "count": len(proxies),
                "skipped": skipped,
                "next_update_ts": sub.get("next_update_ts"),
                "generator_state": copy.deepcopy(generator_state),
            }
        except RuntimeError as exc:
            return _mark_failed(ui_state_dir, state, sub_idx, sub, str(exc), now_ts=now_ts)
        except Exception as exc:
            return _mark_failed(
                ui_state_dir,
                state,
                sub_idx,
                sub,
                f"{type(exc).__name__}: {exc}",
                now_ts=now_ts,
            )


def refresh_due_subscriptions(
    ui_state_dir: str,
    *,
    mihomo_config_file: str,
    restart_xkeen: RestartCallback | None = None,
    restart: bool = True,
    save_callback: SaveCallback | None = None,
) -> List[Dict[str, Any]]:
    state = load_subscription_state(ui_state_dir)
    now_ts = _now()
    results: List[Dict[str, Any]] = []
    for sub in list(state.get("subscriptions") or []):
        if not isinstance(sub, dict) or not bool(sub.get("enabled", True)):
            continue
        try:
            due_ts = float(sub.get("next_update_ts") or 0)
        except Exception:
            due_ts = 0.0
        if due_ts > now_ts:
            continue
        try:
            results.append(
                refresh_subscription(
                    ui_state_dir,
                    str(sub.get("id") or ""),
                    mihomo_config_file=mihomo_config_file,
                    restart_xkeen=restart_xkeen,
                    restart=restart,
                    save_callback=save_callback,
                )
            )
        except Exception as exc:
            results.append({"ok": False, "id": sub.get("id"), "error": str(exc)})
    return results


def start_subscription_scheduler(
    ui_state_dir: str,
    *,
    mihomo_config_file: str,
    restart_xkeen: RestartCallback | None = None,
    save_callback: SaveCallback | None = None,
) -> bool:
    global _SCHEDULER_STARTED

    if not env_flag("XKEEN_MIHOMO_SUBSCRIPTIONS_SCHEDULER", True):
        return False

    with _SCHEDULER_LOCK:
        if _SCHEDULER_STARTED:
            return False
        _SCHEDULER_STARTED = True

    try:
        tick = int(os.environ.get("XKEEN_MIHOMO_SUBSCRIPTIONS_SCHEDULER_TICK", "60") or "60")
    except Exception:
        tick = 60
    tick = max(15, min(3600, tick))

    def _loop() -> None:
        time.sleep(min(15, tick))
        while True:
            try:
                results = refresh_due_subscriptions(
                    ui_state_dir,
                    mihomo_config_file=mihomo_config_file,
                    restart_xkeen=restart_xkeen,
                    restart=True,
                    save_callback=save_callback,
                )
                if results:
                    try:
                        from core.logging import core_log_once

                        core_log_once(
                            "info",
                            "mihomo_subscriptions_auto_refresh",
                            "mihomo subscriptions auto-refresh",
                            total=len(results),
                            ok=sum(1 for r in results if r.get("ok")),
                        )
                    except Exception:
                        pass
            except Exception as exc:
                try:
                    from core.logging import core_log_once

                    core_log_once(
                        "warning",
                        "mihomo_subscriptions_scheduler_failed",
                        "mihomo subscriptions scheduler failed",
                        error=str(exc),
                    )
                except Exception:
                    pass
            time.sleep(tick)

    thread = threading.Thread(target=_loop, name="xkeen-mihomo-subscriptions", daemon=True)
    thread.start()
    return True


__all__ = [
    "DEFAULT_INTERVAL_HOURS",
    "STATE_FILENAME",
    "load_subscription_state",
    "list_subscriptions",
    "refresh_due_subscriptions",
    "refresh_subscription",
    "start_subscription_scheduler",
    "subscription_state_path",
    "sync_from_generator_state",
    "update_subscription_settings",
]
