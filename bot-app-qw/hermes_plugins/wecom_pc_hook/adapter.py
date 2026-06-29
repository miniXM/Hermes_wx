"""WeCom PC DLL-hook platform adapter for Hermes.

The local C ``cli.exe`` keeps the existing simple hook protocol:

* POST /hook/<token> with raw WXWork hook JSON
* GET  /hook/<token> to poll outbound WXWork send payloads

This adapter owns that HTTP surface inside Hermes Gateway, then dispatches
inbound messages through BasePlatformAdapter.handle_message(). That makes the
conversation behave like other Hermes messaging channels instead of like an
external OpenAI-compatible API client.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import logging
import mimetypes
import os
import posixpath
import re
import shutil
import sqlite3
import tempfile
import time
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from xml.etree import ElementTree

try:
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    CRYPTO_AVAILABLE = True
except ImportError:  # pragma: no cover - Hermes normally ships cryptography
    default_backend = None  # type: ignore[assignment]
    Cipher = None  # type: ignore[assignment]
    algorithms = None  # type: ignore[assignment]
    modes = None  # type: ignore[assignment]
    CRYPTO_AVAILABLE = False

try:
    from aiohttp import web

    AIOHTTP_AVAILABLE = True
except ImportError:  # pragma: no cover - Hermes normally ships aiohttp
    web = None  # type: ignore[assignment]
    AIOHTTP_AVAILABLE = False

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
    cache_document_from_bytes,
    cache_media_bytes,
)

logger = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8001
DEFAULT_TOKEN = "testtoken"
DEFAULT_WAKE_WORDS = "@Hermes,Hermes"
MT_RECV_TEXT_MSG = 11041
MT_SEND_TEXT_MSG = 11029
MT_SEND_IMAGE_MSG = 11030
MT_SEND_FILE_MSG = 11031
MT_SEND_VIDEO_MSG = 11067
MT_RECV_MEDIA_MSG = 11042
MT_RECV_STICKER_MSG = 11048
MT_USER_INFO = 11026
MT_CORP_USER_INFO = 11179
DEDUP_TTL_SECONDS = 300
MEDIA_TEMPLATE_ENV = "WECOM_PC_HOOK_MEDIA_TEMPLATE"
MAX_INBOUND_MEDIA_BYTES = int(os.getenv("WECOM_PC_HOOK_MAX_MEDIA_BYTES", str(50 * 1024 * 1024)))
LOCAL_IMAGE_CACHE_WINDOW_SECONDS = int(os.getenv("WECOM_PC_HOOK_LOCAL_IMAGE_WINDOW_SECONDS", "600"))
LOCAL_FILE_CACHE_WINDOW_SECONDS = int(os.getenv("WECOM_PC_HOOK_LOCAL_FILE_WINDOW_SECONDS", str(7 * 24 * 3600)))
LOCAL_IMAGE_CACHE_MAX_FILES = int(os.getenv("WECOM_PC_HOOK_LOCAL_IMAGE_MAX_FILES", "300"))
MEDIA_CAPTION_WAIT_SECONDS = float(os.getenv("WECOM_PC_HOOK_MEDIA_CAPTION_WAIT_SECONDS", "8"))
RECENT_MEDIA_CONTEXT_SECONDS = float(os.getenv("WECOM_PC_HOOK_RECENT_MEDIA_CONTEXT_SECONDS", "120"))
LOCAL_MEDIA_LOOKUP_RETRY_COUNT = int(os.getenv("WECOM_PC_HOOK_LOCAL_MEDIA_RETRY_COUNT", "6"))
LOCAL_MEDIA_LOOKUP_RETRY_DELAY_SECONDS = float(os.getenv("WECOM_PC_HOOK_LOCAL_MEDIA_RETRY_DELAY_SECONDS", "0.8"))
PPTX_MAX_SLIDE_IMAGES = int(os.getenv("WECOM_PC_HOOK_PPTX_MAX_SLIDE_IMAGES", "30"))
PPTX_MAX_IMAGE_BYTES = int(os.getenv("WECOM_PC_HOOK_PPTX_MAX_IMAGE_BYTES", str(12 * 1024 * 1024)))
INTERNAL_ERROR_NOTICE_TTL_SECONDS = 60
UPSTREAM_ERROR_NOTICE = "模型服务暂时不可用，请稍后再试。"
SUPPRESSED_OUTBOUND_PREFIXES = (
    "No home channel is set for Wecom_Pc_Hook",
    "No home channel is set for wecom_pc_hook",
    "Interrupting current task. I'll respond to your message shortly.",
    "First-time tip - I just interrupted my current task",
    "First-time tip — I just interrupted my current task",
)
SUPPRESSED_OUTBOUND_SUBSTRINGS = (
    "Type /sethome to make this chat your home channel",
    "No available channel for model",
)
SUPPRESSED_OUTBOUND_PROGRESS_PATTERNS = (
    re.compile(r"(?is)^working\b.*\biteration\s+\d+/\d+\b"),
    re.compile(r"(?is)\breceiving stream response\b"),
    re.compile(r"(?is)已尝试解决此问题"),
    re.compile(r"(?is)目前处于第\s*\d+\s*次迭代"),
)
SUPPRESSED_OUTBOUND_PROGRESS_MARKERS = (
    "working -",
    "iteration ",
    "receiving stream response",
    "input tokens",
    "output tokens",
    "context window",
    "progress summary",
    "extract_summary.json",
    "markdown:",
    "bytes",
    "输入令牌",
    "输出令牌",
    "已完成步骤",
    "上下文压缩",
    "迭代",
)


@dataclass
class IncomingMessage:
    conversation_id: str
    content: str
    summary: str = ""
    sender: str = ""
    sender_name: str = ""
    receiver: str = ""
    local_id: str = ""
    msg_id: str = ""
    server_id: str = ""
    send_time: str = ""
    message_type: Optional[int] = None
    content_type: Optional[int] = None
    media_kind: str = ""
    is_text: bool = True


@dataclass
class OutboundPayload:
    payload: dict[str, Any]
    conversation_id: str = ""
    echo_content: str = ""


@dataclass
class PendingInboundMedia:
    event: MessageEvent
    sender: str
    created_at: float
    task: asyncio.Task


@dataclass
class RecentInboundMedia:
    media_urls: list[str]
    media_types: list[str]
    message_type: MessageType
    sender: str
    created_at: float


def _truthy(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _string_from(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _number_from(*values: Any) -> Optional[int]:
    for value in values:
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str) and value.strip():
            try:
                return int(value)
            except ValueError:
                continue
    return None


def _record(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _extension(file_name: str) -> str:
    if "." not in file_name:
        return ""
    return file_name.rsplit(".", 1)[-1].lower()


def _normalize_local_path(value: str) -> str:
    candidate = str(value or "").strip()
    if len(candidate) >= 2 and candidate[0] == candidate[-1] and candidate[0] in "`\"'":
        candidate = candidate[1:-1].strip()
    if os.name == "nt" and len(candidate) >= 4 and candidate[0] == "/" and candidate[2] == "/":
        drive = candidate[1]
        if drive.isalpha():
            candidate = f"{drive.upper()}:{candidate[2:]}"
    return candidate


def _format_bytes(size: Optional[int]) -> str:
    if not size or size <= 0:
        return ""
    units = ["B", "KB", "MB", "GB"]
    value = float(size)
    index = 0
    while value >= 1024 and index < len(units) - 1:
        value /= 1024
        index += 1
    return f"{value:.0f} {units[index]}" if index == 0 else f"{value:.1f} {units[index]}"


def _parse_send_time_ms(value: str) -> Optional[float]:
    if not value:
        return None
    try:
        timestamp = float(value)
        if timestamp < 10_000_000_000:
            timestamp *= 1000
        return timestamp
    except ValueError:
        return None


def _month_keys_around(timestamp_ms: Optional[float]) -> list[str]:
    base = datetime.fromtimestamp(timestamp_ms / 1000) if timestamp_ms else datetime.now()
    keys: list[str] = []
    for offset in (-1, 0, 1):
        month = base.month + offset
        year = base.year
        while month <= 0:
            month += 12
            year -= 1
        while month > 12:
            month -= 12
            year += 1
        keys.append(f"{year:04d}-{month:02d}")
    return list(dict.fromkeys(keys))


def _is_account_id(value: str) -> bool:
    return bool(value and value.isdigit() and len(value) >= 8)


def _safe_existing_dir(path_value: str) -> str:
    try:
        resolved = os.path.abspath(os.path.expanduser(path_value))
        return resolved if os.path.isdir(resolved) else ""
    except Exception:
        return ""


def _split_env_paths(name: str) -> list[str]:
    return [part.strip() for part in os.getenv(name, "").split(";") if part.strip()]


def _wecom_image_roots(incoming: IncomingMessage) -> list[str]:
    roots: list[str] = []
    account_ids: list[str] = []
    if incoming.receiver and _is_account_id(incoming.receiver):
        account_ids.append(incoming.receiver)
    match = None
    try:
        import re
        match = re.match(r"^S:(\d+)_", incoming.conversation_id)
    except Exception:
        match = None
    if match and _is_account_id(match.group(1)):
        account_ids.append(match.group(1))
    account_ids = list(dict.fromkeys(account_ids))

    for direct in _split_env_paths("WECOM_PC_HOOK_IMAGE_CACHE_DIR"):
        existing = _safe_existing_dir(direct)
        if existing:
            roots.append(existing)

    bases = _split_env_paths("WECOM_PC_HOOK_DOCUMENT_ROOT")
    user_profile = os.getenv("USERPROFILE")
    if user_profile:
        bases.append(os.path.join(user_profile, "Documents", "WXWork"))
    home_drive = os.getenv("HOMEDRIVE", "")
    home_path = os.getenv("HOMEPATH", "")
    if home_drive or home_path:
        bases.append(os.path.join(f"{home_drive}{home_path}", "Documents", "WXWork"))
    bases.append(os.path.join(os.path.expanduser("~"), "Documents", "WXWork"))

    for base in list(dict.fromkeys(bases)):
        existing_base = _safe_existing_dir(base)
        if not existing_base:
            continue
        if os.path.basename(existing_base).lower() == "image" and os.path.basename(os.path.dirname(existing_base)).lower() == "cache":
            roots.append(existing_base)
            continue
        direct_cache = _safe_existing_dir(os.path.join(existing_base, "Cache", "Image"))
        if direct_cache:
            roots.append(direct_cache)
        for account_id in account_ids:
            account_cache = _safe_existing_dir(os.path.join(existing_base, account_id, "Cache", "Image"))
            if account_cache:
                roots.append(account_cache)
        if not account_ids:
            try:
                for name in os.listdir(existing_base)[:16]:
                    if _is_account_id(name):
                        account_cache = _safe_existing_dir(os.path.join(existing_base, name, "Cache", "Image"))
                        if account_cache:
                            roots.append(account_cache)
            except Exception:
                pass

    return list(dict.fromkeys(roots))


def _wecom_file_roots(incoming: IncomingMessage) -> list[str]:
    roots: list[str] = []
    account_ids: list[str] = []
    if incoming.receiver and _is_account_id(incoming.receiver):
        account_ids.append(incoming.receiver)
    try:
        import re
        match = re.match(r"^S:(\d+)_", incoming.conversation_id)
        if match and _is_account_id(match.group(1)):
            account_ids.append(match.group(1))
    except Exception:
        pass
    account_ids = list(dict.fromkeys(account_ids))

    for direct in _split_env_paths("WECOM_PC_HOOK_FILE_CACHE_DIR"):
        existing = _safe_existing_dir(direct)
        if existing:
            roots.append(existing)

    bases = _split_env_paths("WECOM_PC_HOOK_DOCUMENT_ROOT")
    user_profile = os.getenv("USERPROFILE")
    if user_profile:
        bases.append(os.path.join(user_profile, "Documents", "WXWork"))
    home_drive = os.getenv("HOMEDRIVE", "")
    home_path = os.getenv("HOMEPATH", "")
    if home_drive or home_path:
        bases.append(os.path.join(f"{home_drive}{home_path}", "Documents", "WXWork"))
    bases.append(os.path.join(os.path.expanduser("~"), "Documents", "WXWork"))

    for base in list(dict.fromkeys(bases)):
        existing_base = _safe_existing_dir(base)
        if not existing_base:
            continue
        if os.path.basename(existing_base).lower() == "file" and os.path.basename(os.path.dirname(existing_base)).lower() == "cache":
            roots.append(existing_base)
            continue
        direct_cache = _safe_existing_dir(os.path.join(existing_base, "Cache", "File"))
        if direct_cache:
            roots.append(direct_cache)
        for account_id in account_ids:
            account_cache = _safe_existing_dir(os.path.join(existing_base, account_id, "Cache", "File"))
            if account_cache:
                roots.append(account_cache)
        if not account_ids:
            try:
                for name in os.listdir(existing_base)[:16]:
                    if _is_account_id(name):
                        account_cache = _safe_existing_dir(os.path.join(existing_base, name, "Cache", "File"))
                        if account_cache:
                            roots.append(account_cache)
            except Exception:
                pass

    return list(dict.fromkeys(roots))


def _wecom_cache_mapping_dbs(incoming: IncomingMessage) -> list[str]:
    dbs: list[str] = []
    roots = list(dict.fromkeys(_wecom_image_roots(incoming) + _wecom_file_roots(incoming)))
    for root in roots:
        try:
            normalized = os.path.abspath(root)
            cache_dir = os.path.dirname(normalized)
            if os.path.basename(cache_dir).lower() != "cache":
                continue
            account_dir = os.path.dirname(cache_dir)
            mapping_dir = os.path.join(account_dir, "CacheMapping")
            if not os.path.isdir(mapping_dir):
                continue
            for entry in os.scandir(mapping_dir):
                if entry.is_file() and entry.name.lower().endswith(".db"):
                    dbs.append(entry.path)
        except Exception:
            continue
    return list(dict.fromkeys(dbs))


def _expected_media_md5(payload: dict[str, Any]) -> str:
    data = _record(payload.get("data"))
    cdn = _record(data.get("cdn"))
    file_info = _record(data.get("file"))
    attachment = _record(data.get("attachment"))
    return _string_from(cdn.get("md5"), file_info.get("md5"), attachment.get("md5"), data.get("md5")).lower()


def _declared_media_file_name(payload: dict[str, Any]) -> str:
    data = _record(payload.get("data"))
    cdn = _record(data.get("cdn"))
    file_info = _record(data.get("file"))
    attachment = _record(data.get("attachment"))
    file_name = _string_from(
        cdn.get("file_name"),
        file_info.get("file_name"),
        attachment.get("file_name"),
        data.get("file_name"),
        data.get("filename"),
        data.get("name"),
    )
    return os.path.basename(file_name) if file_name else ""


def _existing_payload_file_paths(payload: dict[str, Any]) -> list[str]:
    paths: list[str] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for item in value.values():
                visit(item)
            return
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, str):
            return
        candidate = _normalize_local_path(value)
        if not candidate:
            return
        try:
            if os.path.isfile(candidate):
                resolved = os.path.abspath(candidate)
                if resolved not in paths:
                    paths.append(resolved)
        except Exception:
            pass

    visit(payload)
    return paths


def _expected_media_sizes(payload: dict[str, Any]) -> list[int]:
    data = _record(payload.get("data"))
    cdn = _record(data.get("cdn"))
    file_info = _record(data.get("file"))
    attachment = _record(data.get("attachment"))
    return [
        size
        for size in (
            _number_from(cdn.get("size")),
            _number_from(cdn.get("file_size")),
            _number_from(cdn.get("md_size")),
            _number_from(cdn.get("ld_size")),
            _number_from(file_info.get("size")),
            _number_from(file_info.get("file_size")),
            _number_from(attachment.get("size")),
            _number_from(data.get("size")),
            _number_from(data.get("file_size")),
        )
        if size and size > 0
    ]


def _cache_mapping_keys(payload: dict[str, Any], kind: str) -> list[str]:
    keys: list[str] = []
    for url in _ordered_media_urls(payload):
        if url and url not in keys:
            keys.append(url)
        if kind == "图片" and url and not url.endswith("_compress"):
            compressed = f"{url}_compress"
            if compressed not in keys:
                keys.append(compressed)
    return keys


def _resolve_cache_mapping_file(db_path: str, file_name: str, kind: str) -> str:
    candidate = _normalize_local_path(file_name)
    if not candidate:
        return ""
    if os.path.isabs(candidate):
        resolved = os.path.abspath(candidate)
        return resolved if os.path.isfile(resolved) else ""

    account_dir = os.path.dirname(os.path.dirname(db_path))
    roots: list[str] = []
    if kind == "图片":
        roots.append(os.path.join(account_dir, "Cache", "Image"))
    else:
        roots.append(os.path.join(account_dir, "Cache", "File"))
    roots.append(account_dir)

    for root in roots:
        resolved = os.path.abspath(os.path.join(root, candidate))
        if os.path.isfile(resolved):
            return resolved
    return ""


def _lookup_cache_mapping_path(
    payload: dict[str, Any],
    incoming: IncomingMessage,
) -> Optional[tuple[str, str]]:
    keys = _cache_mapping_keys(payload, incoming.media_kind)
    expected_md5 = _expected_media_md5(payload)
    timestamp_ms = _parse_send_time_ms(incoming.send_time)
    expected_type = 2 if incoming.media_kind == "图片" else 1
    best: Optional[tuple[float, str, str]] = None

    if not keys and not expected_md5:
        return None

    for db_path in _wecom_cache_mapping_dbs(incoming):
        try:
            connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1)
            cursor = connection.cursor()
            rows: list[tuple[Any, ...]] = []
            if keys:
                placeholders = ",".join("?" for _ in keys)
                query = (
                    "SELECT type, key, file_name, last_modify_time, file_md5 "
                    f"FROM mapping WHERE key IN ({placeholders})"
                )
                rows.extend(cursor.execute(query, keys).fetchall())
            if expected_md5:
                rows.extend(
                    cursor.execute(
                        "SELECT type, key, file_name, last_modify_time, file_md5 FROM mapping WHERE file_md5 = ?",
                        (expected_md5,),
                    ).fetchall()
                )
            connection.close()
        except Exception:
            continue

        seen_rows: set[tuple[Any, ...]] = set()
        for row in rows:
            normalized_row = tuple(row)
            if normalized_row in seen_rows:
                continue
            seen_rows.add(normalized_row)

            type_value, key_value, file_name, last_modify_time, file_md5 = normalized_row
            resolved_path = _resolve_cache_mapping_file(db_path, str(file_name or ""), incoming.media_kind)
            if not resolved_path:
                continue

            score = 0.0
            reasons: list[str] = []
            key_text = str(key_value or "")
            md5_text = str(file_md5 or "").lower()

            if key_text and key_text in keys:
                score += 320.0
                reasons.append("mapping key matched")
            if expected_md5 and md5_text == expected_md5:
                score += 420.0
                reasons.append("mapping md5 matched")
            if incoming.media_kind == "图片" and key_text.endswith("_compress"):
                score += 35.0
                reasons.append("compress variant")
            if _number_from(type_value) == expected_type:
                score += 25.0
                reasons.append("mapping type matched")

            if timestamp_ms:
                mapped_ts = _number_from(last_modify_time)
                if mapped_ts:
                    if mapped_ts > 10_000_000_000:
                        mapped_ts //= 1000
                    delta = abs(mapped_ts * 1000 - timestamp_ms)
                    window_ms = LOCAL_IMAGE_CACHE_WINDOW_SECONDS * 1000
                    if delta <= window_ms:
                        score += max(0.0, 80.0 - (delta / max(window_ms, 1)) * 80.0)
                        reasons.append(f"mapping delta {int(delta)}ms")

            if not best or score > best[0]:
                best = (score, resolved_path, ", ".join(reasons) or "cache mapping")

    if not best:
        return None
    return best[1], best[2]


def _lookup_file_cache_mapping_path(
    payload: dict[str, Any],
    incoming: IncomingMessage,
) -> Optional[tuple[str, str]]:
    expected_name = _declared_media_file_name(payload)
    expected_md5 = _expected_media_md5(payload)
    expected_sizes = _expected_media_sizes(payload)
    timestamp_ms = _parse_send_time_ms(incoming.send_time)
    best: Optional[tuple[float, str, str]] = None
    candidates: list[tuple[str, str, Optional[int]]] = []

    for db_path in _wecom_cache_mapping_dbs(incoming):
        try:
            connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1)
            cursor = connection.cursor()
            rows = cursor.execute(
                "SELECT type, key, file_name, last_modify_time, file_md5 FROM mapping WHERE type = 1"
            ).fetchall()
            connection.close()
        except Exception:
            continue

        for _type_value, _key_value, file_name, last_modify_time, file_md5 in rows:
            resolved_path = _resolve_cache_mapping_file(db_path, str(file_name or ""), incoming.media_kind)
            if not resolved_path:
                continue
            try:
                stat = os.stat(resolved_path)
            except OSError:
                continue
            if stat.st_size <= 0 or stat.st_size > MAX_INBOUND_MEDIA_BYTES:
                continue

            mapped_ts = _number_from(last_modify_time)
            reason_parts = ["file cache mapping"]
            score = 0.0
            base_name = os.path.basename(resolved_path)
            mapped_md5 = str(file_md5 or "").lower()

            if expected_md5 and mapped_md5 == expected_md5:
                score += 600.0
                reason_parts.append("md5 matched")
            if expected_name and base_name.lower() == expected_name.lower():
                score += 220.0
                reason_parts.append("filename matched")
            elif expected_name and expected_name.lower() in base_name.lower():
                score += 130.0
                reason_parts.append("filename contained")

            if expected_sizes:
                preferred = max(expected_sizes)
                diff_ratio = abs(stat.st_size - preferred) / max(stat.st_size, preferred)
                score += max(0.0, 100.0 - diff_ratio * 180.0)
                reason_parts.append(f"size {stat.st_size}/{preferred}")

            if timestamp_ms:
                candidate_ts_ms = float(mapped_ts * 1000 if mapped_ts and mapped_ts < 10_000_000_000 else mapped_ts or stat.st_mtime * 1000)
                delta = abs(candidate_ts_ms - timestamp_ms)
                window_ms = LOCAL_FILE_CACHE_WINDOW_SECONDS * 1000
                if delta <= window_ms:
                    score += max(0.0, 80.0 - (delta / max(window_ms, 1)) * 80.0)
                    reason_parts.append(f"mapping delta {int(delta)}ms")
                elif score < 200.0:
                    candidates.append((resolved_path, ", ".join(reason_parts + [f"mapping delta {int(delta)}ms"]), mapped_ts))
                    continue
            else:
                score += max(0.0, 30.0 - (time.time() - stat.st_mtime) / 3600)
                reason_parts.append("newest mapped file")

            candidates.append((resolved_path, ", ".join(reason_parts), mapped_ts))
            if score > 0 and (not best or score > best[0]):
                best = (score, resolved_path, ", ".join(reason_parts))

    if best:
        return best[1], best[2]

    unique_paths = list(dict.fromkeys(path for path, _reason, _mapped_ts in candidates))
    if len(unique_paths) == 1:
        path = unique_paths[0]
        reason = next((reason for candidate, reason, _ts in candidates if candidate == path), "only file cache mapping")
        return path, reason

    if candidates and not any([expected_name, expected_md5, expected_sizes]):
        def sort_key(item: tuple[str, str, Optional[int]]) -> float:
            path, _reason, mapped_ts = item
            if mapped_ts:
                return float(mapped_ts)
            try:
                return os.stat(path).st_mtime
            except OSError:
                return 0.0

        path, reason, _mapped_ts = max(candidates, key=sort_key)
        return path, f"{reason}, newest file cache mapping"

    return None


def _collect_image_files(root: str, limit: int, depth: int = 0) -> list[str]:
    files: list[str] = []
    try:
        entries = list(os.scandir(root))
        entries.sort(key=lambda entry: _entry_mtime(entry), reverse=True)
        for entry in entries:
            if len(files) >= limit:
                break
            try:
                if entry.is_dir() and depth < 1:
                    files.extend(_collect_image_files(entry.path, limit - len(files), depth + 1))
                elif entry.is_file() and _extension(entry.name) in {"jpg", "jpeg", "png", "gif", "webp", "bmp"}:
                    files.append(entry.path)
            except Exception:
                continue
    except Exception:
        pass
    return files


def _entry_mtime(entry: os.DirEntry) -> float:
    try:
        return entry.stat().st_mtime
    except Exception:
        return 0.0


def _image_dimensions(data: bytes) -> Optional[tuple[int, int]]:
    try:
        if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
            return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
        if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
            return int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")
        if data.startswith(b"\xff\xd8"):
            index = 2
            while index + 9 < len(data):
                if data[index] != 0xFF:
                    index += 1
                    continue
                marker = data[index + 1]
                index += 2
                if marker in {0xD8, 0xD9}:
                    continue
                if index + 2 > len(data):
                    return None
                length = int.from_bytes(data[index : index + 2], "big")
                if length < 2 or index + length > len(data):
                    return None
                if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
                    height = int.from_bytes(data[index + 3 : index + 5], "big")
                    width = int.from_bytes(data[index + 5 : index + 7], "big")
                    return width, height
                index += length
    except Exception:
        return None
    return None


def _image_dimension_score(data: bytes, cdn: dict[str, Any]) -> tuple[float, str]:
    expected_width = _number_from(cdn.get("width"))
    expected_height = _number_from(cdn.get("height"))
    if not expected_width or not expected_height:
        return 0.0, ""
    actual = _image_dimensions(data)
    if not actual:
        return 0.0, ""
    width, height = actual
    expected_pairs = {
        (int(expected_width), int(expected_height)),
        (int(expected_height), int(expected_width)),
    }
    if (width, height) in expected_pairs:
        return 90.0, f"dimensions {width}x{height}"
    width_ratio = min(width / max(expected_width, 1), expected_width / max(width, 1))
    height_ratio = min(height / max(expected_height, 1), expected_height / max(height, 1))
    score = max(0.0, 35.0 * width_ratio * height_ratio)
    return score, f"dimensions {width}x{height}/{int(expected_width)}x{int(expected_height)}"


def _candidate_image_dirs(root: str, timestamp_ms: Optional[float]) -> list[str]:
    dirs = [
        os.path.join(root, key)
        for key in _month_keys_around(timestamp_ms)
        if os.path.isdir(os.path.join(root, key))
    ]
    return dirs or [root]


def _candidate_media_dirs(root: str, timestamp_ms: Optional[float]) -> list[str]:
    dirs = [
        os.path.join(root, key)
        for key in _month_keys_around(timestamp_ms)
        if os.path.isdir(os.path.join(root, key))
    ]
    temp_dir = os.path.join(root, "Temp")
    if os.path.isdir(temp_dir):
        dirs.append(temp_dir)
    return list(dict.fromkeys(dirs or [root]))


def _collect_media_files(root: str, limit: int, depth: int = 0) -> list[str]:
    files: list[str] = []
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                if len(files) >= limit:
                    break
                try:
                    if entry.is_dir() and depth < 1:
                        files.extend(_collect_media_files(entry.path, limit - len(files), depth + 1))
                    elif entry.is_file():
                        files.append(entry.path)
                except Exception:
                    continue
    except Exception:
        pass
    return files


def _score_local_image(path_value: str, data: bytes, cdn: dict[str, Any], timestamp_ms: Optional[float]) -> Optional[tuple[float, str]]:
    try:
        stat = os.stat(path_value)
    except OSError:
        return None
    expected_md5 = _string_from(cdn.get("md5")).lower()
    if expected_md5 and hashlib.md5(data).hexdigest().lower() == expected_md5:
        return 1000.0, "md5 matched"

    score = 0.0
    reasons: list[str] = []
    if timestamp_ms:
        delta = abs(stat.st_mtime * 1000 - timestamp_ms)
        window_ms = LOCAL_IMAGE_CACHE_WINDOW_SECONDS * 1000
        if delta > window_ms:
            return None
        score += max(0.0, 70.0 - (delta / max(window_ms, 1)) * 70.0)
        reasons.append(f"mtime delta {int(delta)}ms")
    else:
        score += max(0.0, 20.0 - (time.time() - stat.st_mtime) / 3600)
        reasons.append("newest local image")

    preferred_sizes = [
        size
        for size in (
            _number_from(cdn.get("size")),
            _number_from(cdn.get("md_size")),
            _number_from(cdn.get("ld_size")),
        )
        if size and size > 0
    ]
    if preferred_sizes:
        preferred = max(preferred_sizes)
        diff_ratio = abs(stat.st_size - preferred) / max(stat.st_size, preferred)
        score += max(0.0, 55.0 - diff_ratio * 110.0)
        reasons.append(f"size {stat.st_size}/{preferred}")
    dimension_score, dimension_reason = _image_dimension_score(data, cdn)
    if dimension_score:
        score += dimension_score
        reasons.append(dimension_reason)
    score += min(8.0, max(0.0, stat.st_size).bit_length() / 3)
    return score, ", ".join(reasons)


async def _find_local_wecom_image(payload: dict[str, Any], incoming: IncomingMessage) -> Optional[tuple[bytes, str, str]]:
    data = _record(payload.get("data"))
    cdn = _record(data.get("cdn"))
    timestamp_ms = _parse_send_time_ms(incoming.send_time)
    best: Optional[tuple[float, str, bytes, str, str]] = None

    mapped = _lookup_cache_mapping_path(payload, incoming)
    if mapped:
        path_value, reason = mapped
        try:
            with open(path_value, "rb") as handle:
                file_bytes = handle.read()
            mime_type = _mime_from_image_bytes(file_bytes)
            if mime_type:
                logger.info("[wecom_pc_hook] matched WeCom cache mapping: %s (%s)", path_value, reason)
                return file_bytes, mime_type, path_value
        except Exception:
            pass

    for root in _wecom_image_roots(incoming):
        for directory in _candidate_image_dirs(root, timestamp_ms):
            for path_value in _collect_image_files(directory, LOCAL_IMAGE_CACHE_MAX_FILES):
                try:
                    if os.path.getsize(path_value) > MAX_INBOUND_MEDIA_BYTES:
                        continue
                    with open(path_value, "rb") as handle:
                        file_bytes = handle.read()
                    mime_type = _mime_from_image_bytes(file_bytes)
                    if not mime_type:
                        continue
                    scored = _score_local_image(path_value, file_bytes, cdn, timestamp_ms)
                    if not scored:
                        continue
                    score, reason = scored
                    if not best or score > best[0]:
                        best = (score, path_value, file_bytes, mime_type, reason)
                except Exception:
                    continue

    if not best:
        return None
    _, path_value, file_bytes, mime_type, reason = best
    logger.info("[wecom_pc_hook] matched local WeCom image cache: %s (%s)", path_value, reason)
    return file_bytes, mime_type, path_value


async def _retry_local_wecom_media_lookup(
    payload: dict[str, Any],
    incoming: IncomingMessage,
    label: str,
    resolver: Callable[[dict[str, Any], IncomingMessage], Awaitable[Optional[tuple[bytes, str, str]]]],
) -> Optional[tuple[bytes, str, str]]:
    attempts = max(1, LOCAL_MEDIA_LOOKUP_RETRY_COUNT)
    delay_seconds = max(0.0, LOCAL_MEDIA_LOOKUP_RETRY_DELAY_SECONDS)
    message_id = incoming.server_id or incoming.local_id or incoming.conversation_id

    for attempt in range(1, attempts + 1):
        resolved = await resolver(payload, incoming)
        if resolved:
            if attempt > 1:
                logger.info(
                    "[wecom_pc_hook] resolved inbound %s from local cache on retry %d/%d for %s",
                    label,
                    attempt,
                    attempts,
                    message_id,
                )
            return resolved
        if attempt < attempts and delay_seconds > 0:
            await asyncio.sleep(delay_seconds)

    return None


def _score_local_media_file(
    path_value: str,
    payload: dict[str, Any],
    incoming: IncomingMessage,
    timestamp_ms: Optional[float],
) -> Optional[tuple[float, str]]:
    data = _record(payload.get("data"))
    cdn = _record(data.get("cdn"))
    file_info = _record(data.get("file"))
    attachment = _record(data.get("attachment"))
    expected_md5 = _string_from(cdn.get("md5"), file_info.get("md5"), attachment.get("md5"), data.get("md5")).lower()
    expected_name = _declared_media_file_name(payload)
    expected_ext = _extension(expected_name)
    expected_sizes = _expected_media_sizes(payload)
    try:
        stat = os.stat(path_value)
    except OSError:
        return None
    if stat.st_size <= 0 or stat.st_size > MAX_INBOUND_MEDIA_BYTES:
        return None

    try:
        if expected_md5:
            with open(path_value, "rb") as handle:
                if hashlib.md5(handle.read()).hexdigest().lower() == expected_md5:
                    return 1000.0, "md5 matched"
    except Exception:
        pass

    score = 0.0
    reasons: list[str] = []
    name = os.path.basename(path_value)
    if expected_name and name.lower() == expected_name.lower():
        score += 120.0
        reasons.append("filename matched")
    elif expected_name and expected_name.lower() in name.lower():
        score += 70.0
        reasons.append("filename contained")
    elif expected_ext and _extension(name) == expected_ext:
        score += 18.0
        reasons.append("extension matched")

    if expected_sizes:
        preferred = max(expected_sizes)
        diff_ratio = abs(stat.st_size - preferred) / max(stat.st_size, preferred)
        if diff_ratio > 0.15 and not reasons:
            return None
        score += max(0.0, 55.0 - diff_ratio * 120.0)
        reasons.append(f"size {stat.st_size}/{preferred}")

    if timestamp_ms:
        delta = abs(stat.st_mtime * 1000 - timestamp_ms)
        window_ms = LOCAL_IMAGE_CACHE_WINDOW_SECONDS * 1000
        if delta > window_ms and score < 100.0:
            return None
        score += max(0.0, 45.0 - (delta / max(window_ms, 1)) * 45.0)
        reasons.append(f"mtime delta {int(delta)}ms")
    else:
        score += max(0.0, 20.0 - (time.time() - stat.st_mtime) / 3600)
        reasons.append("newest local file")

    return (score, ", ".join(reasons)) if score > 0 else None


def _read_local_media_file(path_value: str, reason: str) -> Optional[tuple[bytes, str, str]]:
    try:
        with open(path_value, "rb") as handle:
            file_bytes = handle.read()
        if not file_bytes or len(file_bytes) > MAX_INBOUND_MEDIA_BYTES:
            return None
        mime_type = _mime_from_magic(file_bytes, os.path.basename(path_value))
        logger.info("[wecom_pc_hook] matched local WeCom file cache: %s (%s)", path_value, reason)
        return file_bytes, mime_type, path_value
    except Exception:
        return None


async def _find_local_wecom_media_file(payload: dict[str, Any], incoming: IncomingMessage) -> Optional[tuple[bytes, str, str]]:
    timestamp_ms = _parse_send_time_ms(incoming.send_time)
    best: Optional[tuple[float, str, str]] = None

    for path_value in _existing_payload_file_paths(payload):
        resolved = _read_local_media_file(path_value, "payload file path")
        if resolved:
            return resolved

    mapped = _lookup_cache_mapping_path(payload, incoming)
    if mapped:
        path_value, reason = mapped
        resolved = _read_local_media_file(path_value, reason)
        if resolved:
            return resolved

    mapped_file = _lookup_file_cache_mapping_path(payload, incoming)
    if mapped_file:
        path_value, reason = mapped_file
        resolved = _read_local_media_file(path_value, reason)
        if resolved:
            return resolved

    for root in _wecom_file_roots(incoming):
        for directory in _candidate_media_dirs(root, timestamp_ms):
            for path_value in _collect_media_files(directory, LOCAL_IMAGE_CACHE_MAX_FILES):
                scored = _score_local_media_file(path_value, payload, incoming, timestamp_ms)
                if not scored:
                    continue
                score, reason = scored
                if not best or score > best[0]:
                    best = (score, path_value, reason)

    if not best:
        return None
    _, path_value, reason = best
    return _read_local_media_file(path_value, reason)


def _media_kind(message_type: Optional[int], content_type: Optional[int], file_name: str) -> str:
    if content_type in {29, 101}:
        return "图片"
    ext = _extension(file_name)
    if ext in {"zip", "rar", "7z", "tar", "gz", "bz2", "xz"}:
        return "压缩包"
    if ext in {"doc", "docx", "xls", "xlsx", "ppt", "pptx", "pdf", "txt", "csv", "md"}:
        return "文档"
    if ext in {"jpg", "jpeg", "png", "gif", "bmp", "webp", "heic"}:
        return "图片"
    if ext in {"mp4", "mov", "avi", "mkv", "webm"}:
        return "视频"
    if ext in {"mp3", "wav", "m4a", "aac", "flac", "ogg"}:
        return "音频"
    if message_type == 11042:
        return "媒体"
    return "文件"


def _media_file_name(payload: dict[str, Any], kind: str) -> str:
    data = _record(payload.get("data"))
    cdn = _record(data.get("cdn"))
    file_info = _record(data.get("file"))
    attachment = _record(data.get("attachment"))
    file_name = _string_from(
        cdn.get("file_name"),
        file_info.get("file_name"),
        attachment.get("file_name"),
        data.get("file_name"),
        data.get("filename"),
        data.get("name"),
    )
    if file_name and "." in file_name:
        return os.path.basename(file_name)
    fallback = {
        "图片": "wecom-image.jpg",
        "视频": "wecom-video.mp4",
        "音频": "wecom-audio.ogg",
        "文档": "wecom-document.bin",
        "压缩包": "wecom-archive.zip",
    }.get(kind, "wecom-media.bin")
    if file_name:
        return f"{file_name}-{fallback}"
    return fallback


def _ordered_media_urls(payload: dict[str, Any]) -> list[str]:
    data = _record(payload.get("data"))
    cdn = _record(data.get("cdn"))
    file_info = _record(data.get("file"))
    attachment = _record(data.get("attachment"))
    values = [
        cdn.get("url"),
        cdn.get("md_url"),
        cdn.get("ld_url"),
        file_info.get("url"),
        attachment.get("url"),
        data.get("url"),
    ]
    urls: list[str] = []
    for value in values:
        url = _string_from(value)
        if url and url not in urls:
            urls.append(url)
    return urls


def _mime_from_image_bytes(data: bytes) -> str:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"RIFF") and len(data) >= 12 and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"BM"):
        return "image/bmp"
    return ""


def _mime_from_magic(data: bytes, filename: str, content_type: str = "") -> str:
    image_mime = _mime_from_image_bytes(data)
    if image_mime:
        return image_mime
    if data.startswith(b"%PDF"):
        return "application/pdf"
    if data.startswith(b"PK\x03\x04"):
        ext = _extension(filename)
        if ext == "docx":
            return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        if ext == "xlsx":
            return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        if ext == "pptx":
            return "application/vnd.openxmlformats-officedocument.presentationml.presentation"
        return "application/zip"
    if data.startswith(b"Rar!\x1a\x07"):
        return "application/vnd.rar"
    if data.startswith(b"7z\xbc\xaf\x27\x1c"):
        return "application/x-7z-compressed"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        return "video/mp4"
    if content_type and content_type != "application/octet-stream":
        return content_type
    guessed, _ = mimetypes.guess_type(filename)
    return guessed or content_type or "application/octet-stream"


def _pptx_slide_number(path_value: str) -> int:
    match = re.search(r"/slide(\d+)\.xml$", path_value)
    return int(match.group(1)) if match else 0


def _pptx_slide_rel_path(slide_path: str) -> str:
    slide_dir, slide_name = posixpath.split(slide_path)
    return posixpath.join(slide_dir, "_rels", f"{slide_name}.rels")


def _pptx_resolve_target(base_path: str, target: str) -> str:
    target = str(target or "").strip()
    if not target:
        return ""
    if target.startswith("/"):
        return target.lstrip("/")
    return posixpath.normpath(posixpath.join(posixpath.dirname(base_path), target))


def _pptx_relationship_targets(raw_xml: bytes) -> dict[str, str]:
    try:
        root = ElementTree.fromstring(raw_xml)
    except Exception:
        return {}
    targets: dict[str, str] = {}
    for rel in root:
        rel_id = rel.attrib.get("Id") or rel.attrib.get("id")
        target = rel.attrib.get("Target")
        if rel_id and target:
            targets[rel_id] = target
    return targets


def _pptx_image_reference_order(raw_xml: bytes) -> list[str]:
    try:
        root = ElementTree.fromstring(raw_xml)
    except Exception:
        return []
    ordered: list[str] = []
    for element in root.iter():
        for key, value in element.attrib.items():
            if key.endswith("}embed") or key.endswith("}link") or key in {"r:embed", "r:link"}:
                if value and value not in ordered:
                    ordered.append(value)
    return ordered


def _cache_pptx_slide_images(data: bytes, file_name: str) -> tuple[list[str], list[str], int]:
    if _extension(file_name) != "pptx" or not data.startswith(b"PK\x03\x04"):
        return [], [], 0

    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return [], [], 0

    cached_paths: list[str] = []
    media_types: list[str] = []
    seen_media: set[str] = set()
    slide_count = 0
    prefix = os.path.splitext(os.path.basename(file_name or "presentation.pptx"))[0] or "presentation"

    with archive:
        names = set(archive.namelist())
        slide_paths = sorted(
            (
                name
                for name in names
                if name.startswith("ppt/slides/slide") and name.endswith(".xml")
            ),
            key=_pptx_slide_number,
        )
        slide_count = len(slide_paths)

        for slide_index, slide_path in enumerate(slide_paths, start=1):
            if len(cached_paths) >= PPTX_MAX_SLIDE_IMAGES:
                break
            rel_path = _pptx_slide_rel_path(slide_path)
            if rel_path not in names:
                continue
            try:
                rel_targets = _pptx_relationship_targets(archive.read(rel_path))
                refs = _pptx_image_reference_order(archive.read(slide_path))
            except Exception:
                continue

            image_candidates: list[tuple[int, str]] = []
            for ref_id in refs:
                target = _pptx_resolve_target(slide_path, rel_targets.get(ref_id, ""))
                if target.startswith("ppt/media/") and target in names and target not in seen_media:
                    try:
                        size = archive.getinfo(target).file_size
                    except Exception:
                        size = 0
                    image_candidates.append((size, target))

            if not image_candidates:
                continue

            image_candidates.sort(reverse=True)
            _, media_path = image_candidates[0]
            seen_media.add(media_path)
            try:
                info = archive.getinfo(media_path)
                if info.file_size <= 0 or info.file_size > PPTX_MAX_IMAGE_BYTES:
                    continue
                image_bytes = archive.read(media_path)
            except Exception:
                continue

            image_mime = _mime_from_image_bytes(image_bytes)
            if not image_mime:
                continue

            image_ext = os.path.splitext(media_path)[1].lower() or mimetypes.guess_extension(image_mime) or ".png"
            cached = cache_media_bytes(
                image_bytes,
                filename=f"{prefix}-slide-{slide_index:02d}{image_ext}",
                mime_type=image_mime,
                default_kind="image",
            )
            if not cached:
                continue
            cached_paths.append(cached.path)
            media_types.append(cached.media_type)

    return cached_paths, media_types, slide_count


def _default_media_kind(kind: str) -> str:
    if kind == "图片":
        return "image"
    if kind == "视频":
        return "video"
    if kind == "音频":
        return "audio"
    return "document"


def _message_type_for_media(kind: str, media_types: list[str]) -> MessageType:
    if kind == "图片" or any(media_type.startswith("image/") for media_type in media_types):
        return MessageType.PHOTO
    if kind == "视频" or any(media_type.startswith("video/") for media_type in media_types):
        return MessageType.VIDEO
    if kind == "音频" or any(media_type.startswith("audio/") for media_type in media_types):
        return MessageType.AUDIO
    return MessageType.DOCUMENT


def _strip_pkcs7(data: bytes) -> bytes:
    if not data:
        return data
    pad = data[-1]
    if pad <= 0 or pad > 16 or len(data) < pad:
        return data
    if data[-pad:] != bytes([pad]) * pad:
        return data
    return data[:-pad]


def _aes_decrypt(data: bytes, key: bytes, mode_obj: Any) -> bytes:
    if not CRYPTO_AVAILABLE or Cipher is None or algorithms is None or default_backend is None:
        raise RuntimeError("cryptography is not available")
    cipher = Cipher(algorithms.AES(key), mode_obj, backend=default_backend())
    decryptor = cipher.decryptor()
    return _strip_pkcs7(decryptor.update(data) + decryptor.finalize())


def _decrypt_candidates(data: bytes, aes_key: str) -> list[bytes]:
    if not aes_key or not CRYPTO_AVAILABLE or modes is None:
        return []
    candidates: list[tuple[bytes, list[Any]]] = []
    if len(aes_key) == 32:
        if all(ch in "0123456789abcdefABCDEF" for ch in aes_key):
            key = bytes.fromhex(aes_key)
            candidates.append((key, [modes.CBC(bytes(16)), modes.CBC(key), modes.ECB()]))
        ascii_key = aes_key.encode("utf-8", errors="ignore")
        if len(ascii_key) in {16, 24, 32}:
            candidates.append((ascii_key, [modes.CBC(ascii_key[:16]), modes.CBC(bytes(16)), modes.ECB()]))
    try:
        normalized = aes_key.replace("-", "+").replace("_", "/")
        normalized += "=" * ((4 - len(normalized) % 4) % 4)
        decoded = base64.b64decode(normalized)
        if len(decoded) in {16, 24, 32}:
            candidates.append((decoded, [modes.CBC(decoded[:16]), modes.CBC(bytes(16)), modes.ECB()]))
    except Exception:
        pass

    results: list[bytes] = []
    for key, candidate_modes in candidates:
        for mode_obj in candidate_modes:
            try:
                if len(data) % 16 != 0:
                    continue
                decrypted = _aes_decrypt(data, key, mode_obj)
                if decrypted and decrypted not in results:
                    results.append(decrypted)
            except Exception:
                continue
    return results


def _media_summary(payload: dict[str, Any]) -> tuple[str, Optional[int], str]:
    data = _record(payload.get("data"))
    cdn = _record(data.get("cdn"))
    file_info = _record(data.get("file"))
    attachment = _record(data.get("attachment"))
    message_type = _number_from(payload.get("type"))
    content_type = _number_from(data.get("content_type"), payload.get("content_type"))
    file_name = _string_from(
        cdn.get("file_name"),
        file_info.get("file_name"),
        attachment.get("file_name"),
        data.get("file_name"),
        data.get("filename"),
        data.get("name"),
    )
    kind = _media_kind(message_type, content_type, file_name)
    width = _number_from(cdn.get("width"), data.get("width"))
    height = _number_from(cdn.get("height"), data.get("height"))
    size = _number_from(
        cdn.get("size"),
        cdn.get("file_size"),
        cdn.get("md_size"),
        file_info.get("size"),
        file_info.get("file_size"),
        attachment.get("size"),
        data.get("size"),
        data.get("file_size"),
    )
    md5 = _string_from(cdn.get("md5"), file_info.get("md5"), attachment.get("md5"), data.get("md5"))
    has_url = bool(
        _string_from(
            cdn.get("url"),
            cdn.get("md_url"),
            cdn.get("ld_url"),
            file_info.get("url"),
            attachment.get("url"),
            data.get("url"),
        )
    )

    parts = [f"[{kind}]"]
    if file_name:
        parts.append(f"文件名：{file_name}")
    if width and height:
        parts.append(f"尺寸：{width}x{height}")
    formatted_size = _format_bytes(size)
    if formatted_size:
        parts.append(f"大小：{formatted_size}")
    if md5:
        parts.append(f"md5：{md5}")
    if content_type is not None:
        parts.append(f"content_type：{content_type}")
    if message_type is not None:
        parts.append(f"message_type：{message_type}")
    if has_url:
        parts.append("包含 CDN 下载地址")
    return "，".join(parts), content_type, kind


def _extract_incoming(payload: dict[str, Any]) -> IncomingMessage:
    data = _record(payload.get("data"))
    message_type = _number_from(payload.get("type"))
    is_text = message_type is None or message_type == MT_RECV_TEXT_MSG
    content = _string_from(data.get("content"), payload.get("content"))
    summary = content
    content_type = _number_from(data.get("content_type"), payload.get("content_type"))
    media_kind = ""
    if not is_text:
        summary, content_type, media_kind = _media_summary(payload)

    return IncomingMessage(
        conversation_id=_string_from(data.get("conversation_id"), payload.get("conversation_id")),
        content=content.strip(),
        summary=summary.strip(),
        sender=_string_from(data.get("sender"), payload.get("sender")),
        sender_name=_string_from(data.get("sender_name"), payload.get("sender_name")),
        receiver=_string_from(data.get("receiver"), payload.get("receiver")),
        local_id=_string_from(data.get("local_id"), payload.get("local_id")),
        msg_id=_string_from(data.get("msgid"), payload.get("msgid")),
        server_id=_string_from(data.get("server_id"), payload.get("server_id")),
        send_time=_string_from(data.get("send_time"), payload.get("send_time")),
        message_type=message_type,
        content_type=content_type,
        media_kind=media_kind,
        is_text=is_text,
    )


def _download_bytes_sync(url: str) -> tuple[bytes, str]:
    request = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) HermesWeComPlugin/1.0",
            "Accept": "*/*",
        },
    )
    with urlopen(request, timeout=30) as response:
        length = response.headers.get("Content-Length")
        if length:
            try:
                content_length = int(length)
                if content_length > MAX_INBOUND_MEDIA_BYTES:
                    raise ValueError(f"media too large: {length} bytes")
            except ValueError as error:
                if "media too large" in str(error):
                    raise error
        data = response.read(MAX_INBOUND_MEDIA_BYTES + 1)
        if len(data) > MAX_INBOUND_MEDIA_BYTES:
            raise ValueError(f"media too large: > {MAX_INBOUND_MEDIA_BYTES} bytes")
        content_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0].strip()
        return data, content_type


async def _cache_inbound_media(payload: dict[str, Any], incoming: IncomingMessage) -> tuple[list[str], list[str], list[str]]:
    if incoming.is_text:
        return [], [], []

    file_name = _media_file_name(payload, incoming.media_kind)
    urls = _ordered_media_urls(payload)
    data = _record(payload.get("data"))
    cdn = _record(data.get("cdn"))
    aes_key = _string_from(cdn.get("aes_key"))
    download_errors: list[str] = []

    if incoming.media_kind == "图片":
        local_image = await _retry_local_wecom_media_lookup(
            payload,
            incoming,
            "image",
            _find_local_wecom_image,
        )
        if local_image:
            local_bytes, local_mime, local_path = local_image
            cached = cache_media_bytes(
                local_bytes,
                filename=os.path.basename(local_path),
                mime_type=local_mime,
                default_kind="image",
            )
            if cached:
                logger.info(
                    "[wecom_pc_hook] cached inbound image for %s: source=%s cached=%s media_type=%s",
                    incoming.server_id or incoming.local_id or incoming.conversation_id,
                    local_path,
                    cached.path,
                    cached.media_type,
                )
                return [cached.path], [cached.media_type], []
            copied_path = os.path.join(tempfile.gettempdir(), f"wecom-image-{uuid.uuid4().hex}.jpg")
            await asyncio.to_thread(shutil.copyfile, local_path, copied_path)
            logger.info(
                "[wecom_pc_hook] copied inbound image for %s: source=%s cached=%s media_type=%s",
                incoming.server_id or incoming.local_id or incoming.conversation_id,
                local_path,
                copied_path,
                local_mime,
            )
            return [copied_path], [local_mime], []

    for url in urls:
        try:
            raw, content_type = await asyncio.to_thread(_download_bytes_sync, url)
            mime_type = _mime_from_magic(raw, file_name, content_type)
            candidates = [(raw, mime_type)]
            for decrypted in _decrypt_candidates(raw, aes_key):
                candidates.append((decrypted, _mime_from_magic(decrypted, file_name, content_type)))
            for candidate_bytes, candidate_mime in candidates:
                if not candidate_bytes:
                    continue
                pptx_image_urls, pptx_image_types, pptx_slide_count = _cache_pptx_slide_images(candidate_bytes, file_name)
                if pptx_image_urls:
                    logger.info(
                        "[wecom_pc_hook] extracted %d/%d PPTX slide image(s) for %s from url=%s",
                        len(pptx_image_urls),
                        pptx_slide_count,
                        incoming.server_id or incoming.local_id or incoming.conversation_id,
                        url[:72],
                    )
                    return pptx_image_urls, pptx_image_types, []
                cached = cache_media_bytes(
                    candidate_bytes,
                    filename=file_name,
                    mime_type=candidate_mime,
                    default_kind=_default_media_kind(incoming.media_kind),
                )
                if cached:
                    logger.info(
                        "[wecom_pc_hook] cached inbound media for %s: url=%s cached=%s media_type=%s",
                        incoming.server_id or incoming.local_id or incoming.conversation_id,
                        url[:72],
                        cached.path,
                        cached.media_type,
                    )
                    return [cached.path], [cached.media_type], []
                if incoming.media_kind not in {"图片"}:
                    fallback_path = cache_document_from_bytes(candidate_bytes, file_name)
                    fallback_mime = _mime_from_magic(candidate_bytes, file_name, candidate_mime)
                    logger.info(
                        "[wecom_pc_hook] cached inbound document fallback for %s: url=%s cached=%s media_type=%s",
                        incoming.server_id or incoming.local_id or incoming.conversation_id,
                        url[:72],
                        fallback_path,
                        fallback_mime,
                    )
                    return [fallback_path], [fallback_mime], []
            download_errors.append(f"{url[:72]} -> unsupported payload")
        except Exception as error:
            download_errors.append(f"{url[:72]} -> {error}")

    if incoming.media_kind != "图片":
        local_media = await _retry_local_wecom_media_lookup(
            payload,
            incoming,
            incoming.media_kind or "media",
            _find_local_wecom_media_file,
        )
        if local_media:
            local_bytes, local_mime, local_path = local_media
            pptx_image_urls, pptx_image_types, pptx_slide_count = _cache_pptx_slide_images(local_bytes, os.path.basename(local_path) or file_name)
            if pptx_image_urls:
                logger.info(
                    "[wecom_pc_hook] extracted %d/%d PPTX slide image(s) for %s from source=%s",
                    len(pptx_image_urls),
                    pptx_slide_count,
                    incoming.server_id or incoming.local_id or incoming.conversation_id,
                    local_path,
                )
                return pptx_image_urls, pptx_image_types, []
            cached = cache_media_bytes(
                local_bytes,
                filename=os.path.basename(local_path),
                mime_type=local_mime,
                default_kind=_default_media_kind(incoming.media_kind),
            )
            if cached:
                logger.info(
                    "[wecom_pc_hook] cached inbound file for %s: source=%s cached=%s media_type=%s",
                    incoming.server_id or incoming.local_id or incoming.conversation_id,
                    local_path,
                    cached.path,
                    cached.media_type,
                )
                return [cached.path], [cached.media_type], []
            copied_path = os.path.join(tempfile.gettempdir(), f"wecom-media-{uuid.uuid4().hex}-{os.path.basename(local_path)}")
            await asyncio.to_thread(shutil.copyfile, local_path, copied_path)
            logger.info(
                "[wecom_pc_hook] copied inbound file for %s: source=%s cached=%s media_type=%s",
                incoming.server_id or incoming.local_id or incoming.conversation_id,
                local_path,
                copied_path,
                local_mime,
            )
            return [copied_path], [local_mime], []

    if download_errors:
        logger.warning(
            "[%s] inbound media cache failed for %s: %s",
            "wecom_pc_hook",
            incoming.server_id or incoming.local_id or incoming.conversation_id,
            " | ".join(download_errors),
        )
    return [], [], download_errors


def _parse_wake_words(value: Any) -> list[str]:
    if isinstance(value, list):
        words = [str(item).strip() for item in value]
    else:
        words = [part.strip() for part in str(value or DEFAULT_WAKE_WORDS).split(",")]
    return [word for word in words if word]


def _strip_wake_words(text: str, wake_words: list[str]) -> str:
    next_text = text
    for word in wake_words:
        next_text = next_text.replace(word, "")
    return next_text.strip() or text


def _internal_delivery_notice(text: str) -> tuple[str, str]:
    normalized = str(text or "").strip()
    if not normalized:
        return "", ""
    if normalized.startswith("⏳ Retrying in") or normalized.startswith("Retrying in"):
        return "retry", ""
    if (
        normalized.startswith("❌ API failed after")
        or normalized.startswith("API call failed after")
        or "No available channel for model" in normalized
    ):
        return "error", UPSTREAM_ERROR_NOTICE
    return "", ""


def _should_suppress_outbound_text(text: str) -> bool:
    normalized = str(text or "").strip()
    if not normalized:
        return False
    if normalized == UPSTREAM_ERROR_NOTICE:
        return True
    if any(normalized.startswith(prefix) for prefix in SUPPRESSED_OUTBOUND_PREFIXES):
        return True
    if any(snippet in normalized for snippet in SUPPRESSED_OUTBOUND_SUBSTRINGS):
        return True
    if any(pattern.search(normalized) for pattern in SUPPRESSED_OUTBOUND_PROGRESS_PATTERNS):
        return True
    progress_hits = sum(1 for marker in SUPPRESSED_OUTBOUND_PROGRESS_MARKERS if marker in normalized.lower())
    if progress_hits >= 2:
        return True
    if progress_hits >= 1 and "\n" in normalized and len(normalized) >= 80:
        return True
    return False


def _dump_payload(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _make_wxwork_text_payload(conversation_id: str, content: str) -> dict[str, Any]:
    return {
        "type": MT_SEND_TEXT_MSG,
        "data": {
            "conversation_id": conversation_id,
            "content": content,
        },
    }


def _payload_text(payload: dict[str, Any]) -> str:
    data = _record(payload.get("data"))
    return _string_from(data.get("content"), payload.get("content"))


def _merge_media_caption(existing: str, caption: str) -> str:
    base = str(existing or "").strip()
    next_text = str(caption or "").strip()
    if not next_text:
        return base
    if not base:
        return next_text
    if next_text in base:
        return base
    return f"{base}\n\n{next_text}"


def _is_generic_media_prompt(text: str) -> bool:
    return not str(text or "").strip()


def _make_wxwork_file_payload(message_type: int, conversation_id: str, file_path: str) -> dict[str, Any]:
    return _apply_media_template(
        message_type=message_type,
        conversation_id=conversation_id,
        file_path=file_path,
        media_kind="file",
    )


def _apply_media_template(
    message_type: int,
    conversation_id: str,
    file_path: str,
    media_kind: str,
) -> dict[str, Any]:
    template = _load_media_template()
    if template:
        return _fill_template(
            template,
            {
                "type": message_type,
                "conversation_id": conversation_id,
                "file_path": file_path,
                "path": file_path,
                "file": file_path,
                "media_kind": media_kind,
                "file_name": os.path.basename(file_path),
            },
        )
    return {
        "type": message_type,
        "data": {
            "conversation_id": conversation_id,
            "file": file_path,
            "path": file_path,
            "file_path": file_path,
            "media_kind": media_kind,
            "file_name": os.path.basename(file_path),
        },
    }


def _load_media_template() -> Optional[dict[str, Any]]:
    raw = os.getenv(MEDIA_TEMPLATE_ENV, "").strip()
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        logger.warning("Invalid %s JSON: %s", MEDIA_TEMPLATE_ENV, error)
        return None
    return value if isinstance(value, dict) else None


def _fill_template(value: Any, replacements: dict[str, Any]) -> Any:
    if isinstance(value, str):
        if value.startswith("{") and value.endswith("}") and value.count("{") == 1 and value.count("}") == 1:
            key = value[1:-1]
            if key in replacements:
                return replacements[key]
        result = value
        for key, replacement in replacements.items():
            result = result.replace("{" + key + "}", str(replacement))
        return result
    if isinstance(value, list):
        return [_fill_template(item, replacements) for item in value]
    if isinstance(value, dict):
        return {str(key): _fill_template(item, replacements) for key, item in value.items()}
    return value


def check_requirements() -> bool:
    return AIOHTTP_AVAILABLE


def validate_config(config: PlatformConfig) -> bool:
    extra = getattr(config, "extra", {}) or {}
    return _truthy(extra.get("enabled"), default=bool(getattr(config, "enabled", False)))


def is_connected(config: PlatformConfig) -> bool:
    return validate_config(config)


def _env_enablement() -> dict | None:
    if not _truthy(os.getenv("WECOM_PC_HOOK_ENABLED"), default=False):
        return None
    seed = {
        "enabled": True,
        "host": os.getenv("WECOM_PC_HOOK_HOST", DEFAULT_HOST),
        "port": int(os.getenv("WECOM_PC_HOOK_PORT", str(DEFAULT_PORT))),
        "token": os.getenv("WECOM_PC_HOOK_TOKEN", DEFAULT_TOKEN),
        "reply_mode": os.getenv("WECOM_PC_HOOK_REPLY_MODE", "all"),
        "wake_words": os.getenv("WECOM_PC_HOOK_WAKE_WORDS", DEFAULT_WAKE_WORDS),
        "allow_all_users": _truthy(os.getenv("WECOM_PC_HOOK_ALLOW_ALL_USERS"), default=True),
    }
    home_channel = os.getenv("WECOM_PC_HOOK_HOME_CHANNEL", "").strip()
    if home_channel:
        seed["home_channel"] = {
            "platform": "wecom_pc_hook",
            "chat_id": home_channel,
            "name": os.getenv("WECOM_PC_HOOK_HOME_CHANNEL_NAME", "Home"),
            "thread_id": os.getenv("WECOM_PC_HOOK_HOME_CHANNEL_THREAD_ID") or None,
        }
    return seed


def _apply_yaml_config(yaml_cfg: dict, platform_cfg: dict) -> dict:
    del yaml_cfg
    seed: dict[str, Any] = {}
    extra = _record(platform_cfg.get("extra"))
    for key in (
        "enabled",
        "host",
        "port",
        "token",
        "reply_mode",
        "wake_words",
        "allow_all_users",
        "send_image_type",
        "send_file_type",
        "send_video_type",
    ):
        if key in extra:
            seed[key] = extra[key]
        elif key in platform_cfg:
            seed[key] = platform_cfg[key]

    home = _record(platform_cfg.get("home_channel"))
    if home.get("chat_id"):
        seed["home_channel"] = {
            "platform": "wecom_pc_hook",
            "chat_id": str(home["chat_id"]),
            "name": str(home.get("name") or "Home"),
            "thread_id": home.get("thread_id") or None,
        }
    return seed


def _media_send_types_from_config(config: PlatformConfig) -> tuple[int, int, int]:
    extra = getattr(config, "extra", {}) or {}
    image_type = _number_from(extra.get("send_image_type"), os.getenv("WECOM_PC_HOOK_SEND_IMAGE_TYPE")) or MT_SEND_IMAGE_MSG
    file_type = _number_from(extra.get("send_file_type"), os.getenv("WECOM_PC_HOOK_SEND_FILE_TYPE")) or MT_SEND_FILE_MSG
    video_type = _number_from(extra.get("send_video_type"), os.getenv("WECOM_PC_HOOK_SEND_VIDEO_TYPE")) or MT_SEND_VIDEO_MSG
    return image_type, file_type, video_type


def _media_payload_for_file(config: PlatformConfig, chat_id: str, file_path: str, is_voice: bool = False) -> dict[str, Any]:
    image_type, file_type, video_type = _media_send_types_from_config(config)
    ext = os.path.splitext(str(file_path))[1].lower()
    if not is_voice and ext in {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".heic"}:
        return _apply_media_template(image_type, chat_id, file_path, "image")
    if not is_voice and ext in {".mp4", ".mov", ".avi", ".mkv", ".webm"}:
        return _apply_media_template(video_type, chat_id, file_path, "video")
    if is_voice:
        return _apply_media_template(file_type, chat_id, file_path, "voice")
    return _apply_media_template(file_type, chat_id, file_path, "document")


def _standalone_endpoint(config: PlatformConfig) -> tuple[str, str, int, str]:
    extra = getattr(config, "extra", {}) or {}
    host = str(extra.get("host") or os.getenv("WECOM_PC_HOOK_HOST") or DEFAULT_HOST)
    port = int(extra.get("port") or os.getenv("WECOM_PC_HOOK_PORT") or DEFAULT_PORT)
    token = str(extra.get("token") or os.getenv("WECOM_PC_HOOK_TOKEN") or DEFAULT_TOKEN)
    return f"http://{host}:{port}/send/{token}", host, port, token


def _post_standalone_payload(config: PlatformConfig, payload: dict[str, Any]) -> None:
    url, _, _, _ = _standalone_endpoint(config)
    data = json.dumps({"payload": payload}, ensure_ascii=False).encode("utf-8")
    request = Request(
        url,
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urlopen(request, timeout=10) as response:
        if response.status >= 400:
            raise RuntimeError(f"HTTP {response.status}")


async def _standalone_send(
    config: PlatformConfig,
    chat_id: str,
    message: str,
    *,
    thread_id: Optional[str] = None,
    media_files=None,
    force_document: bool = False,
) -> dict[str, Any]:
    del thread_id, force_document

    def _send_sync() -> dict[str, Any]:
        sent = 0
        target = str(chat_id)
        text = str(message or "").strip()
        if text:
            _post_standalone_payload(config, _make_wxwork_text_payload(target, text))
            sent += 1
        for media_path, is_voice in media_files or []:
            local_path = _normalize_local_path(str(media_path))
            if not os.path.isfile(local_path):
                return {"error": f"media path does not exist: {media_path}"}
            _post_standalone_payload(
                config,
                _media_payload_for_file(config, target, os.path.abspath(local_path), bool(is_voice)),
            )
            sent += 1
        return {"success": True, "platform": "wecom_pc_hook", "chat_id": target, "sent": sent}

    return await asyncio.to_thread(_send_sync)


class WeComPcHookAdapter(BasePlatformAdapter):
    MAX_MESSAGE_LENGTH = 4096

    def __init__(self, config: PlatformConfig):
        super().__init__(config=config, platform=Platform("wecom_pc_hook"))
        extra = config.extra or {}
        self.host = str(extra.get("host") or os.getenv("WECOM_PC_HOOK_HOST") or DEFAULT_HOST)
        self.port = int(extra.get("port") or os.getenv("WECOM_PC_HOOK_PORT") or DEFAULT_PORT)
        self.token = str(extra.get("token") or os.getenv("WECOM_PC_HOOK_TOKEN") or DEFAULT_TOKEN)
        self.reply_mode = str(extra.get("reply_mode") or os.getenv("WECOM_PC_HOOK_REPLY_MODE") or "all").lower()
        if self.reply_mode not in {"all", "wake", "off"}:
            self.reply_mode = "all"
        self.wake_words = _parse_wake_words(extra.get("wake_words") or os.getenv("WECOM_PC_HOOK_WAKE_WORDS"))
        self.allow_all_users = _truthy(
            extra.get("allow_all_users", os.getenv("WECOM_PC_HOOK_ALLOW_ALL_USERS")),
            default=True,
        )
        self.send_image_type = _number_from(
            extra.get("send_image_type"),
            os.getenv("WECOM_PC_HOOK_SEND_IMAGE_TYPE"),
        ) or MT_SEND_IMAGE_MSG
        self.send_file_type = _number_from(
            extra.get("send_file_type"),
            os.getenv("WECOM_PC_HOOK_SEND_FILE_TYPE"),
        ) or MT_SEND_FILE_MSG
        self.send_video_type = _number_from(
            extra.get("send_video_type"),
            os.getenv("WECOM_PC_HOOK_SEND_VIDEO_TYPE"),
        ) or MT_SEND_VIDEO_MSG

        self._app: Optional[web.Application] = None
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None
        self._outbound: asyncio.Queue[OutboundPayload] = asyncio.Queue()
        self._seen: dict[str, float] = {}
        self._bot_ids: set[str] = set()
        self._recent_outbound: dict[str, float] = {}
        self._last_internal_error_notice: dict[str, float] = {}
        self._pending_inbound_media: dict[str, PendingInboundMedia] = {}
        self._recent_inbound_media: dict[str, RecentInboundMedia] = {}
        self._last_poll_at: float = 0.0
        self._last_post_at: float = 0.0
        self._last_inbound_at: float = 0.0
        self._last_outbound_at: float = 0.0

    async def connect(self) -> bool:
        if not AIOHTTP_AVAILABLE:
            logger.warning("[%s] aiohttp is not installed", self.name)
            return False
        if self._runner:
            return True

        self._app = web.Application()
        self._app.router.add_post("/hook/{token}", self._handle_post)
        self._app.router.add_get("/hook/{token}", self._handle_get)
        self._app.router.add_post("/send/{token}", self._handle_send)
        self._app.router.add_post("/control/{token}", self._handle_control)
        self._app.router.add_get("/health", self._handle_health)
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.host, self.port)
        await self._site.start()
        self._mark_connected()
        logger.info(
            "[%s] Listening on http://%s:%s/hook/<token> reply_mode=%s",
            self.name,
            self.host,
            self.port,
            self.reply_mode,
        )
        return True

    async def disconnect(self) -> None:
        self._running = False
        self._mark_disconnected()
        if self._runner:
            await self._runner.cleanup()
        self._runner = None
        self._site = None
        self._app = None
        self._seen.clear()
        self._recent_outbound.clear()
        self._pending_inbound_media.clear()
        self._recent_inbound_media.clear()

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> SendResult:
        del reply_to, metadata
        text = str(content or "").strip()
        if not text:
            return SendResult(success=True, message_id=None)
        if _should_suppress_outbound_text(text):
            logger.info(
                "[%s] suppressed internal outbound notice for %s: %s",
                self.name,
                chat_id,
                text.replace("\r", " ").replace("\n", " ")[:500],
            )
            return SendResult(success=True, message_id=None)
        for chunk in self.truncate_message(text, self.MAX_MESSAGE_LENGTH):
            await self._queue_payload(
                _make_wxwork_text_payload(str(chat_id), chunk),
                conversation_id=str(chat_id),
                echo_content=chunk,
            )
        return SendResult(success=True, message_id=uuid.uuid4().hex[:12])

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> SendResult:
        del reply_to
        url = str(image_url or "").strip()
        if not url:
            return SendResult(success=False, error="image_url is required")
        try:
            local_path = await self._download_remote_file(url, default_suffix=".jpg")
        except Exception as error:
            logger.error("[%s] Failed to download image %s: %s", self.name, url, error)
            fallback = f"{caption}\n{url}" if caption else url
            return await self.send(chat_id=chat_id, content=fallback)
        return await self.send_image_file(
            chat_id=chat_id,
            image_path=local_path,
            caption=caption,
            metadata=metadata,
            _trusted_path=True,
        )

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        del reply_to, metadata
        trusted_path = bool(kwargs.pop("_trusted_path", False))
        return await self._send_media_file(
            chat_id=chat_id,
            file_path=image_path,
            message_type=self.send_image_type,
            caption=caption,
            trusted_path=trusted_path,
            media_kind="image",
            label="image",
        )

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        del file_name, reply_to, metadata, kwargs
        return await self._send_media_file(
            chat_id=chat_id,
            file_path=file_path,
            message_type=self.send_file_type,
            caption=caption,
            media_kind="document",
            label="document",
        )

    async def send_video(
        self,
        chat_id: str,
        video_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        del reply_to, metadata, kwargs
        return await self._send_media_file(
            chat_id=chat_id,
            file_path=video_path,
            message_type=self.send_video_type,
            caption=caption,
            media_kind="video",
            label="video",
        )

    async def send_voice(
        self,
        chat_id: str,
        audio_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        del reply_to, metadata, kwargs
        return await self._send_media_file(
            chat_id=chat_id,
            file_path=audio_path,
            message_type=self.send_file_type,
            caption=caption,
            media_kind="voice",
            label="voice",
        )

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        del chat_id, metadata

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        return {"name": chat_id, "type": self._chat_type(chat_id), "chat_id": chat_id}

    async def _handle_health(self, request: web.Request) -> web.Response:
        del request
        return web.json_response(
            {
                "ok": True,
                "service": "wecom_pc_hook",
                "reply_mode": self.reply_mode,
                "allow_all_users": self.allow_all_users,
                "queue": self._outbound.qsize(),
                "last_poll_at": self._last_poll_at,
                "last_post_at": self._last_post_at,
                "last_inbound_at": self._last_inbound_at,
                "last_outbound_at": self._last_outbound_at,
                "media": {
                    "enabled": True,
                    "send_image_type": self.send_image_type,
                    "send_file_type": self.send_file_type,
                    "send_video_type": self.send_video_type,
                    "template_env": MEDIA_TEMPLATE_ENV,
                },
            }
        )

    async def _handle_send(self, request: web.Request) -> web.Response:
        if not self._token_allowed(request.match_info.get("token", "")):
            return web.Response(text="Forbidden", status=403)
        try:
            body = await request.json()
        except Exception:
            return web.Response(text="Invalid JSON", status=400)
        if not isinstance(body, dict):
            return web.Response(text="Invalid JSON", status=400)
        if isinstance(body.get("payload"), dict):
            await self._queue_payload(body["payload"])
            return web.Response(text="Queued raw payload")
        if isinstance(body.get("type"), int) and isinstance(body.get("data"), dict):
            await self._queue_payload({"type": body["type"], "data": body["data"]})
            return web.Response(text="Queued raw payload")
        conversation_id = _string_from(body.get("conversation_id"), body.get("conversationId"))
        content = _string_from(body.get("content"))
        if not conversation_id or not content:
            return web.Response(text="conversation_id and content are required", status=400)
        await self.send(conversation_id, content)
        return web.Response(text="Queued")

    async def _handle_control(self, request: web.Request) -> web.Response:
        if not self._token_allowed(request.match_info.get("token", "")):
            return web.Response(text="Forbidden", status=403)
        try:
            body = await request.json()
        except Exception:
            return web.Response(text="Invalid JSON", status=400)
        if not isinstance(body, dict):
            return web.Response(text="Invalid JSON", status=400)

        reply_mode = body.get("reply_mode")
        if reply_mode is not None:
            next_mode = str(reply_mode).strip().lower()
            if next_mode not in {"all", "wake", "off"}:
                return web.Response(text="Invalid reply_mode", status=400)
            self.reply_mode = next_mode

        wake_words = body.get("wake_words")
        if wake_words is not None:
            self.wake_words = _parse_wake_words(wake_words)

        allow_all_users = body.get("allow_all_users")
        if allow_all_users is not None:
            self.allow_all_users = _truthy(allow_all_users, default=True)

        logger.info(
            "[%s] Updated reply policy: mode=%s wake_words=%s allow_all_users=%s",
            self.name,
            self.reply_mode,
            ",".join(self.wake_words),
            self.allow_all_users,
        )
        return web.json_response(
            {
                "ok": True,
                "reply_mode": self.reply_mode,
                "wake_words": self.wake_words,
                "allow_all_users": self.allow_all_users,
            }
        )

    async def _handle_get(self, request: web.Request) -> web.Response:
        if not self._token_allowed(request.match_info.get("token", "")):
            return web.Response(text="Forbidden", status=403)
        self._last_poll_at = time.time()
        try:
            outbound = self._outbound.get_nowait()
        except asyncio.QueueEmpty:
            return web.Response(status=204)
        notice_kind, notice_text = _internal_delivery_notice(outbound.echo_content)
        if notice_kind == "retry":
            return web.Response(status=204)
        if notice_kind == "error":
            now = time.time()
            notice_key = f"{outbound.conversation_id}:{notice_text}"
            last_sent = self._last_internal_error_notice.get(notice_key, 0.0)
            if now - last_sent < INTERNAL_ERROR_NOTICE_TTL_SECONDS:
                return web.Response(status=204)
            self._last_internal_error_notice[notice_key] = now
            if notice_text and notice_text != outbound.echo_content:
                outbound = OutboundPayload(
                    payload=_make_wxwork_text_payload(outbound.conversation_id, notice_text),
                    conversation_id=outbound.conversation_id,
                    echo_content=notice_text,
                )
        delivered_text = _payload_text(outbound.payload)
        if outbound.conversation_id and delivered_text:
            self._remember_outbound(outbound.conversation_id, delivered_text)
        self._last_outbound_at = time.time()
        return web.Response(
            text=_dump_payload(outbound.payload),
            content_type="application/json",
            charset="utf-8",
        )

    async def _handle_post(self, request: web.Request) -> web.Response:
        if not self._token_allowed(request.match_info.get("token", "")):
            return web.Response(text="Forbidden", status=403)
        self._last_post_at = time.time()
        try:
            body = await request.json()
        except Exception:
            return web.Response(text="Invalid JSON", status=400)
        if not isinstance(body, dict):
            return web.Response(text="Invalid JSON", status=400)

        self._remember_bot_identity(body)
        incoming = _extract_incoming(body)
        if not incoming.conversation_id:
            return web.Response(text="Ignored")
        self._last_inbound_at = time.time()

        if incoming.sender and incoming.sender in self._bot_ids:
            logger.debug("[%s] Ignored self message from %s", self.name, incoming.sender)
            return web.Response(text="Ignored self")

        if incoming.content and self._is_recent_outbound(incoming.conversation_id, incoming.content):
            logger.debug("[%s] Ignored outbound echo in %s", self.name, incoming.conversation_id)
            return web.Response(text="Ignored echo")

        dedupe_key = self._dedupe_key(incoming)
        if self._is_duplicate(dedupe_key):
            return web.Response(text="Ignored duplicate")

        if incoming.content and not self._should_reply(incoming.content):
            logger.debug(
                "[%s] Ignored message by reply strategy %s in %s",
                self.name,
                self.reply_mode,
                incoming.conversation_id,
            )
            return web.Response(text="Ignored by policy")

        text = _strip_wake_words(incoming.content, self.wake_words) if self.reply_mode == "wake" else incoming.content
        if self.reply_mode == "wake" and not incoming.is_text and not text.strip():
            text = incoming.content
        if self.reply_mode == "wake" and incoming.is_text and not text.strip():
            logger.debug("[%s] Ignored wake-only message in %s", self.name, incoming.conversation_id)
            return web.Response(text="Ignored wake only")

        media_urls, media_types, media_errors = await _cache_inbound_media(body, incoming)
        if not text.strip() and not media_urls:
            if media_errors:
                logger.info(
                    "[%s] ignored media-only event after cache failure for %s",
                    self.name,
                    incoming.server_id or incoming.local_id or incoming.conversation_id,
                )
            return web.Response(text="Ignored empty media")

        source = self.build_source(
            chat_id=incoming.conversation_id,
            chat_name=incoming.conversation_id,
            chat_type=self._chat_type(incoming.conversation_id),
            user_id=incoming.sender or incoming.receiver or incoming.conversation_id,
            user_name=incoming.sender_name or incoming.sender or incoming.conversation_id,
            message_id=incoming.server_id or incoming.msg_id or incoming.local_id or None,
        )

        message_type = MessageType.TEXT
        if not incoming.is_text:
            message_type = _message_type_for_media(incoming.media_kind, media_types)

        if media_errors and not media_urls:
            logger.info(
                "[%s] dispatching text-only event after media cache failure for %s",
                self.name,
                incoming.server_id or incoming.local_id or incoming.conversation_id,
            )
            message_type = MessageType.TEXT

        if incoming.is_text:
            pending = self._pending_inbound_media.pop(incoming.conversation_id, None)
            if pending and (not pending.sender or pending.sender == incoming.sender):
                pending.task.cancel()
                pending.event.text = _merge_media_caption(pending.event.text, text)
                pending.event.raw_message = {
                    "media": pending.event.raw_message,
                    "caption": body,
                }
                pending.event.message_id = incoming.server_id or pending.event.message_id
                pending.event.timestamp = self._parse_timestamp(incoming.send_time)
                logger.info(
                    "[%s] merged text caption into pending media for %s",
                    self.name,
                    incoming.conversation_id,
                )
                await self.handle_message(pending.event)
                return web.Response(text="Queued media caption")
            if pending:
                self._pending_inbound_media[incoming.conversation_id] = pending

            recent_media = self._get_recent_media(incoming.conversation_id, incoming.sender)
            if recent_media and not media_urls:
                media_urls = list(recent_media.media_urls)
                media_types = list(recent_media.media_types)
                message_type = recent_media.message_type
                self._recent_inbound_media.pop(incoming.conversation_id, None)
                if _is_generic_media_prompt(text):
                    text = incoming.content
                logger.info(
                    "[%s] attached recent media context to text for %s",
                    self.name,
                    incoming.conversation_id,
                )

        event = MessageEvent(
            text=text,
            message_type=message_type,
            source=source,
            raw_message=body,
            message_id=incoming.server_id or incoming.msg_id or incoming.local_id or uuid.uuid4().hex[:12],
            media_urls=media_urls,
            media_types=media_types,
            timestamp=self._parse_timestamp(incoming.send_time),
        )

        if not incoming.is_text and media_urls and incoming.media_kind == "图片":
            previous = self._pending_inbound_media.pop(incoming.conversation_id, None)
            if previous:
                previous.task.cancel()
                self._remember_recent_media(incoming.conversation_id, previous.event, previous.sender)
                await self.handle_message(previous.event)
            task = asyncio.create_task(self._dispatch_media_event_later(incoming.conversation_id))
            self._pending_inbound_media[incoming.conversation_id] = PendingInboundMedia(
                event=event,
                sender=incoming.sender,
                created_at=time.time(),
                task=task,
            )
            logger.info(
                "[%s] queued inbound image for caption window %.1fs in %s",
                self.name,
                MEDIA_CAPTION_WAIT_SECONDS,
                incoming.conversation_id,
            )
            return web.Response(text="Queued media")

        if media_urls:
            self._remember_recent_media(incoming.conversation_id, event, incoming.sender)
        await self.handle_message(event)
        return web.Response(text="Queued")

    def _token_allowed(self, token: str) -> bool:
        return not self.token or token == self.token

    def _should_reply(self, content: str) -> bool:
        if self.reply_mode == "off":
            return False
        if self.reply_mode == "wake":
            return any(word and word in content for word in self.wake_words)
        return True

    def _chat_type(self, conversation_id: str) -> str:
        normalized = conversation_id.lower()
        if "room" in normalized or normalized.startswith("r:") or normalized.startswith("g:"):
            return "group"
        return "dm"

    def _parse_timestamp(self, value: str) -> datetime:
        if not value:
            return datetime.now()
        try:
            timestamp = int(value)
            if timestamp > 10_000_000_000:
                timestamp = timestamp // 1000
            return datetime.fromtimestamp(timestamp)
        except Exception:
            return datetime.now()

    def _remember_bot_identity(self, payload: dict[str, Any]) -> None:
        if _number_from(payload.get("type")) not in {MT_USER_INFO, MT_CORP_USER_INFO}:
            return
        data = _record(payload.get("data"))
        for candidate in (
            data.get("user_id"),
            data.get("acctid"),
            data.get("account"),
            payload.get("user_id"),
            payload.get("acctid"),
            payload.get("account"),
        ):
            value = _string_from(candidate)
            if value:
                self._bot_ids.add(value)
        if self._bot_ids:
            logger.info("[%s] Known WeCom self ids: %s", self.name, ", ".join(sorted(self._bot_ids)))

    def _compact_seen(self) -> None:
        cutoff = time.time() - DEDUP_TTL_SECONDS
        if len(self._seen) > 1000:
            self._seen = {key: seen_at for key, seen_at in self._seen.items() if seen_at >= cutoff}
        if len(self._recent_outbound) > 1000:
            self._recent_outbound = {
                key: seen_at for key, seen_at in self._recent_outbound.items() if seen_at >= cutoff
            }
        media_cutoff = time.time() - max(RECENT_MEDIA_CONTEXT_SECONDS, MEDIA_CAPTION_WAIT_SECONDS, 1.0)
        if len(self._recent_inbound_media) > 1000:
            self._recent_inbound_media = {
                key: item for key, item in self._recent_inbound_media.items() if item.created_at >= media_cutoff
            }

    def _remember_recent_media(self, conversation_id: str, event: MessageEvent, sender: str) -> None:
        if not event.media_urls:
            return
        self._compact_seen()
        self._recent_inbound_media[conversation_id] = RecentInboundMedia(
            media_urls=list(event.media_urls),
            media_types=list(event.media_types),
            message_type=event.message_type,
            sender=sender,
            created_at=time.time(),
        )

    def _get_recent_media(self, conversation_id: str, sender: str) -> Optional[RecentInboundMedia]:
        self._compact_seen()
        item = self._recent_inbound_media.get(conversation_id)
        if not item:
            return None
        if item.sender and sender and item.sender != sender:
            return None
        if time.time() - item.created_at > RECENT_MEDIA_CONTEXT_SECONDS:
            self._recent_inbound_media.pop(conversation_id, None)
            return None
        return item

    async def _dispatch_media_event_later(self, conversation_id: str) -> None:
        current_task = asyncio.current_task()
        try:
            await asyncio.sleep(max(MEDIA_CAPTION_WAIT_SECONDS, 0.0))
            pending = self._pending_inbound_media.get(conversation_id)
            if not pending or pending.task is not current_task:
                return
            self._pending_inbound_media.pop(conversation_id, None)
            self._remember_recent_media(conversation_id, pending.event, pending.sender)
            await self.handle_message(pending.event)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[%s] delayed media dispatch failed for %s", self.name, conversation_id)

    def _is_duplicate(self, key: str) -> bool:
        self._compact_seen()
        if key in self._seen:
            return True
        self._seen[key] = time.time()
        return False

    def _dedupe_key(self, incoming: IncomingMessage) -> str:
        source_id = incoming.server_id or incoming.msg_id or incoming.local_id
        if source_id:
            return f"{incoming.conversation_id}:{source_id}"
        return ":".join(
            [
                incoming.conversation_id,
                incoming.sender,
                incoming.receiver,
                incoming.send_time,
                incoming.content,
            ]
        )

    def _remember_outbound(self, conversation_id: str, content: str) -> None:
        self._compact_seen()
        self._recent_outbound[f"{conversation_id}:{content}"] = time.time()

    def _is_recent_outbound(self, conversation_id: str, content: str) -> bool:
        self._compact_seen()
        return f"{conversation_id}:{content}" in self._recent_outbound

    async def _queue_payload(
        self,
        payload: dict[str, Any],
        conversation_id: str = "",
        echo_content: str = "",
    ) -> None:
        if conversation_id and echo_content:
            self._remember_outbound(conversation_id, echo_content)
        await self._outbound.put(
            OutboundPayload(
                payload=payload,
                conversation_id=conversation_id,
                echo_content=echo_content,
            )
        )

    async def _send_media_file(
        self,
        chat_id: str,
        file_path: str,
        message_type: int,
        caption: Optional[str],
        media_kind: str,
        label: str,
        trusted_path: bool = False,
    ) -> SendResult:
        resolved_path = self._resolve_media_path(file_path, trusted_path=trusted_path)
        if not resolved_path:
            return SendResult(
                success=False,
                error=f"{label} path is invalid or not allowed: {file_path}",
            )
        if caption:
            await self.send(chat_id=chat_id, content=caption)
        await self._queue_payload(
            _apply_media_template(message_type, str(chat_id), resolved_path, media_kind),
            conversation_id=str(chat_id),
        )
        return SendResult(success=True, message_id=uuid.uuid4().hex[:12])

    def _resolve_media_path(self, file_path: str, trusted_path: bool = False) -> str:
        candidate = _normalize_local_path(str(file_path or ""))
        if not candidate:
            return ""
        if trusted_path:
            resolved = os.path.abspath(candidate)
            return resolved if os.path.isfile(resolved) else ""
        safe_path = self.validate_media_delivery_path(candidate)
        if safe_path:
            return safe_path
        resolved = os.path.abspath(candidate)
        return resolved if os.path.isfile(resolved) else ""

    async def _download_remote_file(self, url: str, default_suffix: str = ".bin") -> str:
        return await asyncio.to_thread(self._download_remote_file_sync, url, default_suffix)

    def _download_remote_file_sync(self, url: str, default_suffix: str = ".bin") -> str:
        request = Request(url, headers={"User-Agent": "Hermes-WeCom-PC-Hook/1.0"})
        with urlopen(request, timeout=30) as response:
            data = response.read()
            if not data:
                raise ValueError("empty response body")
            content_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0].strip()
        suffix = self._download_suffix(url, content_type, default_suffix)
        target_dir = os.path.join(tempfile.gettempdir(), "hermes-wecom-pc-hook")
        os.makedirs(target_dir, exist_ok=True)
        target_path = os.path.join(target_dir, f"{uuid.uuid4().hex}{suffix}")
        with open(target_path, "wb") as handle:
            handle.write(data)
        logger.info("[%s] Downloaded remote media to %s", self.name, target_path)
        return target_path

    def _download_suffix(self, url: str, content_type: str, default_suffix: str) -> str:
        parsed = urlparse(url)
        suffix = os.path.splitext(parsed.path)[1]
        if suffix:
            return suffix
        guessed = mimetypes.guess_extension(content_type) if content_type else None
        return guessed or default_suffix


def register(ctx) -> None:
    ctx.register_platform(
        name="wecom_pc_hook",
        label="WeCom PC Hook",
        adapter_factory=lambda cfg: WeComPcHookAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=[],
        install_hint="aiohttp is required; it is bundled with Hermes on this machine",
        env_enablement_fn=_env_enablement,
        apply_yaml_config_fn=_apply_yaml_config,
        cron_deliver_env_var="WECOM_PC_HOOK_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,
        allowed_users_env="WECOM_PC_HOOK_ALLOWED_USERS",
        allow_all_env="WECOM_PC_HOOK_ALLOW_ALL_USERS",
        max_message_length=WeComPcHookAdapter.MAX_MESSAGE_LENGTH,
        pii_safe=False,
        emoji="WX",
        platform_hint=(
            "You are assisting a user in Enterprise WeChat. "
            "Act like a document-focused assistant, avoid mentioning implementation details or device type, "
            "use plain text, and keep replies concise."
        ),
    )
