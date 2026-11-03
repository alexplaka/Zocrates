"""Opt-in diagnostic snapshot builder and multi-layer privacy redaction engine.

Generates sanitized, GitHub-ready diagnostic reports strictly on user request.
All disk and hardware inspection helpers are pure read-only operations safe for
worker thread execution without mutating configuration files or databases.
"""

from __future__ import annotations

import contextlib
import copy
import datetime
import json
import logging
import os
import platform
import re
import sys
import urllib.parse
from pathlib import Path
from typing import Any

from google.adk.events._rewind_events import _apply_rewinds

from zocrates import __version__
import zocrates.config as _zocrates_cfg
from zocrates.config import (
    BACKEND_PORT,
    DEFAULT_CONFIG,
    ZOCRATES_DATA_SUBDIR,
    _CONFIG_CACHE,
    _CONFIG_CACHE_FINGERPRINT,
    _CONFIG_CACHE_LOCK,
    get_auth_token,
    get_datalab_api_key,
    get_openalex_api_key,
    get_secure_api_key,
    resolve_zotero_data_dir,
)
from zocrates.rag.hardware import (
    get_docling_hardware_assessment,
    get_system_hardware_info,
)
from zocrates.utils.session_formatting import (
    _is_compaction_event,
    extract_event_parts,
    format_author_name,
)

logger = logging.getLogger(__name__)

# Hard report ceiling (50 KB) to stay comfortably within GitHub's ~65 KB issue body cap.
MAX_REPORT_BYTES: int = 50 * 1024
TRUNCATION_MARKER: str = "... [truncated to maintain 50 KB report ceiling]"

# Log tail safety limits.
MIN_LOG_TAIL_LINES: int = 50
MAX_LOG_TAIL_LINES: int = 500
DEFAULT_LOG_TAIL_LINES: int = 250
MAX_LOG_LINE_CHARS: int = 500
LOG_TAIL_SEEK_BYTES: int = 256 * 1024  # 256 KB binary tail window

# Privacy placeholder messages.
OMITTED_PROMPTS_TEXT: str = "<User prompt and assistant message text omitted for privacy>"
OMITTED_LOGS_TEXT: str = (
    "<Backend log tail omitted (enable via checkbox if needed for debugging)>"
)
OMITTED_TITLE_TEXT: str = "<Title omitted>"

# Explicit technical configuration allowlist (Plan §3B).
ALLOWED_CONFIG_KEYS: frozenset[str] = frozenset(
    {
        "llm_provider",
        "llm_model",
        "coordinator_thinking",
        "subagent_thinking",
        "embedding_mode",
        "embedding_model",
        "embedding_dimension",
        "document_converter_id",
        "docling_mode",
        "datalab_merge_cross_page",
        "citations_enabled",
        "references_section_enabled",
        "auto_render_mermaid_diagrams",
        "enable_mcp_server",
        "citation_verification_enabled",
        "citation_verification_provider",
        "citation_verification_model",
        "citation_verification_threshold",
        "adaptive_compaction_enabled",
        "session_retention_days",
        "session_max_count",
    }
)

# Pre-compiled Layer 2 redaction regexes.
_OPENROUTER_KEY_RE = re.compile(r"sk-or-v1-[A-Za-z0-9_\-]{8,}")
_GENERIC_SK_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}")
_BEARER_TOKEN_RE = re.compile(r"(?i)(\bBearer\s+)(?!\[REDACTED)([^\s\"',;)}\]]+)")
_ZOCRATES_AUTH_HEADER_RE = re.compile(
    r"(?i)(\bX-Zocrates-Auth\s*[:=]\s*)(?!\[REDACTED)([^\s\"',;)}\]]+)"
)
_X_API_KEY_HEADER_RE = re.compile(
    r"(?i)(\bX-Api-Key\s*[:=]\s*)(?!\[REDACTED)([^\s\"',;)}\]]+)"
)
_QUERY_TOKEN_RE = re.compile(
    r"(?i)([?&]?(?:token|api_key|apikey|access_token|auth_token)=)(?!\[REDACTED)([^\s&\"',;)}\]]+)"
)
_URL_USERINFO_RE = re.compile(r"(https?://)([^/\s:@]+:[^/\s@]+)@")
_MAILTO_RE = re.compile(r"(?i)\bmailto:[^\s\"',;>)\]]+")
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")

# Multi-OS user home and UNC path patterns.
_LINUX_VAR_HOME_RE = re.compile(r"(?<![A-Za-z0-9_\-])(/var/home/)([^/\s\"':;<>|\\]+)")
_LINUX_HOME_RE = re.compile(r"(?<![A-Za-z0-9_\-])(/home/)([^/\s\"':;<>|\\]+)")
_MACOS_USERS_RE = re.compile(r"(?<![A-Za-z0-9_\-])(/Users/)([^/\s\"':;<>|\\]+)")
_WIN_ESCAPED_USERS_RE = re.compile(
    r"(?<![A-Za-z0-9_\-])[A-Za-z]:\\\\Users\\\\[^\\\s\"':;<>|/]+"
)
_WIN_BACKSLASH_USERS_RE = re.compile(
    r"(?<![A-Za-z0-9_\-])[A-Za-z]:\\Users\\[^\\\s\"':;<>|/]+"
)
_WIN_SLASH_USERS_RE = re.compile(
    r"(?<![A-Za-z0-9_\-])[A-Za-z]:/Users/[^/\s\"':;<>|\\]+"
)
_WIN_ESCAPED_UNC_RE = re.compile(
    r"(?<![\\/\w:])\\\\\\\\[A-Za-z0-9._\-]+\\\\[A-Za-z0-9._$\-]+"
)
_WIN_UNC_RE = re.compile(
    r"(?<![\\/\w:])\\\\[A-Za-z0-9._\-]+\\[A-Za-z0-9._$\-]+"
)


def _resolve_zotero_dir_readonly(zotero_data_dir: Path | str | None = None) -> Path | None:
    """Resolve Zotero data directory without creating directories on disk."""
    if zotero_data_dir is not None:
        return Path(zotero_data_dir)
    with contextlib.suppress(Exception):
        resolved = resolve_zotero_data_dir(None)
        if resolved is not None:
            return Path(resolved)
    return None


def _read_config_readonly(zotero_data_dir: Path | str | None = None) -> dict[str, Any]:
    """Read Zocrates configuration without acquiring FileLock or writing backfills to disk."""
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    resolved_dir = _resolve_zotero_dir_readonly(zotero_data_dir)
    if resolved_dir is None:
        return cfg

    config_path = resolved_dir / ZOCRATES_DATA_SUBDIR / "zocrates_config.json"
    if not config_path.is_file():
        return cfg

    try:
        resolved_path = config_path.resolve()
        stat_res = config_path.stat()
        fingerprint = (stat_res.st_mtime_ns, stat_res.st_size)
        with _CONFIG_CACHE_LOCK:
            if (
                resolved_path in _CONFIG_CACHE
                and _CONFIG_CACHE_FINGERPRINT.get(resolved_path) == fingerprint
            ):
                cached = copy.deepcopy(_CONFIG_CACHE[resolved_path])
                cfg.update(cached)
                return cfg
    except OSError:
        return cfg

    try:
        with open(config_path, encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            cfg.update(raw)
    except (OSError, json.JSONDecodeError):
        pass
    return cfg


def _collect_exact_secrets(resolved_dir: Path | None) -> list[tuple[str, str]]:
    """Collect active runtime secrets as (secret_value, redaction_placeholder) pairs."""
    candidates: list[tuple[str, str]] = []

    def _add(val: str | None, placeholder: str) -> None:
        if not val or not isinstance(val, str):
            return
        cleaned = val.strip()
        if len(cleaned) >= 4:
            candidates.append((cleaned, placeholder))

    # 1. Backend auth token (purely read-only: never create backend.auth or mutate _CACHED_AUTH_TOKEN).
    with contextlib.suppress(Exception):
        env_tok = os.getenv("ZOCRATES_AUTH_TOKEN", "").strip()
        if env_tok:
            _add(env_tok, "[REDACTED_AUTH_TOKEN]")
        cached_tok = getattr(_zocrates_cfg, "_CACHED_AUTH_TOKEN", None)
        if isinstance(cached_tok, str) and cached_tok.strip():
            _add(cached_tok, "[REDACTED_AUTH_TOKEN]")
        if resolved_dir is not None:
            sidecar_path = resolved_dir / ZOCRATES_DATA_SUBDIR / "backend.auth"
            if sidecar_path.is_file():
                _add(
                    sidecar_path.read_text(encoding="utf-8").strip(),
                    "[REDACTED_AUTH_TOKEN]",
                )

    # 2. Live API keys from environment/memory.
    with contextlib.suppress(Exception):
        _add(get_secure_api_key(), "[REDACTED_API_KEY]")
    with contextlib.suppress(Exception):
        _add(
            get_secure_api_key("openai_api_key", ("OPENAI_API_KEY",)),
            "[REDACTED_API_KEY]",
        )
    with contextlib.suppress(Exception):
        _add(get_datalab_api_key(resolved_dir), "[REDACTED_API_KEY]")
    with contextlib.suppress(Exception):
        _add(get_openalex_api_key(resolved_dir), "[REDACTED_API_KEY]")

    # 3. Configured polite-pool email if present in config.
    with contextlib.suppress(Exception):
        raw_cfg = _read_config_readonly(resolved_dir)
        polite_email = raw_cfg.get("scholar_polite_email")
        if isinstance(polite_email, str) and polite_email.strip():
            _add(polite_email, "[REDACTED_EMAIL]")

    # Sort longest-first so shorter substrings never partially clobber longer keys.
    dedup: dict[str, str] = {}
    for secret, placeholder in sorted(candidates, key=lambda item: len(item[0]), reverse=True):
        if secret not in dedup:
            dedup[secret] = placeholder
    return list(dedup.items())


def _collect_path_variants(resolved_dir: Path | None) -> list[tuple[str, str]]:
    """Pre-collect path variants for resolved_dir and Path.home()."""
    variants: list[tuple[str, str]] = []
    if resolved_dir is not None:
        paths_to_check = [resolved_dir]
        with contextlib.suppress(OSError):
            r = resolved_dir.resolve()
            if r != resolved_dir:
                paths_to_check.append(r)
        for p in paths_to_check:
            native = str(p).rstrip("/\\")
            posix = p.as_posix().rstrip("/")
            for candidate in (native.replace("\\", "\\\\"), native, posix):
                if candidate and len(candidate) > 2 and candidate not in ("/", "\\", "."):
                    if not any(candidate == v[0] for v in variants):
                        variants.append((candidate, "<ZOTERO_DATA_DIR>"))

    with contextlib.suppress(Exception):
        home_path = Path.home()
        paths_to_check = [home_path]
        with contextlib.suppress(OSError):
            r = home_path.resolve()
            if r != home_path:
                paths_to_check.append(r)
        for p in paths_to_check:
            native = str(p).rstrip("/\\")
            posix = p.as_posix().rstrip("/")
            for candidate in (native.replace("\\", "\\\\"), native, posix):
                if candidate and len(candidate) > 2 and candidate not in ("/", "\\", "."):
                    if not any(candidate == v[0] for v in variants):
                        variants.append((candidate, "<HOME>"))

    variants.sort(key=lambda item: len(item[0]), reverse=True)
    return variants


def _replace_path_variants(text: str, target_path: Path, placeholder: str) -> str:
    """Replace native, POSIX, and JSON-escaped variants of ``target_path`` in ``text``."""
    variants: list[str] = []
    paths_to_check = [target_path]
    with contextlib.suppress(OSError):
        resolved = target_path.resolve()
        if resolved != target_path:
            paths_to_check.append(resolved)

    for p in paths_to_check:
        native = str(p).rstrip("/\\")
        posix = p.as_posix().rstrip("/")
        for candidate in (
            native.replace("\\", "\\\\"),
            native,
            posix,
        ):
            if candidate and candidate not in ("/", "\\", ".") and len(candidate) > 2:
                if candidate not in variants:
                    variants.append(candidate)

    variants.sort(key=len, reverse=True)
    for variant in variants:
        if variant in text:
            text = text.replace(variant, placeholder)
    return text


def sanitize_text(
    text: str | None,
    zotero_data_dir: Path | str | None = None,
    *,
    exact_secrets: list[tuple[str, str]] | None = None,
    path_variants: list[tuple[str, str]] | None = None,
) -> str:
    """Scrub secrets, tokens, emails, and multi-OS filesystem paths from ``text``.

    Applies two independent layers:
      1. Exact-value replacement for live auth tokens, API keys, ``zotero_data_dir``,
         and ``Path.home()``.
      2. Pre-compiled regex scrubbing for OpenRouter/LLM keys, Bearer/Auth headers,
         query tokens, URL credentials, emails, and Linux/macOS/Windows/UNC paths.
    """
    if text is None:
        return ""
    out = str(text)
    if not out:
        return ""

    resolved_dir = _resolve_zotero_dir_readonly(zotero_data_dir)

    # Layer 1A: Exact secret replacement.
    secrets_list = exact_secrets if exact_secrets is not None else _collect_exact_secrets(resolved_dir)
    for secret, placeholder in secrets_list:
        if secret in out:
            out = out.replace(secret, placeholder)
        url_encoded = urllib.parse.quote(secret, safe="")
        if url_encoded != secret and len(url_encoded) >= 6 and url_encoded in out:
            out = out.replace(url_encoded, placeholder)

    # Layer 1B: Exact filesystem root replacement (<ZOTERO_DATA_DIR> before <HOME>).
    variants_list = path_variants if path_variants is not None else _collect_path_variants(resolved_dir)
    for variant, placeholder in variants_list:
        if variant in out:
            out = out.replace(variant, placeholder)

    # Layer 2: Pattern-based scrubbing.
    out = _OPENROUTER_KEY_RE.sub("[REDACTED_OPENROUTER_KEY]", out)
    out = _GENERIC_SK_KEY_RE.sub("[REDACTED_API_KEY]", out)
    out = _BEARER_TOKEN_RE.sub(r"\1[REDACTED_TOKEN]", out)
    out = _ZOCRATES_AUTH_HEADER_RE.sub(r"\1[REDACTED_AUTH_TOKEN]", out)
    out = _X_API_KEY_HEADER_RE.sub(r"\1[REDACTED_API_KEY]", out)
    out = _QUERY_TOKEN_RE.sub(r"\1[REDACTED_TOKEN]", out)
    out = _URL_USERINFO_RE.sub(r"\1[REDACTED_CREDENTIALS]@", out)
    out = _MAILTO_RE.sub("mailto:[REDACTED_EMAIL]", out)
    out = _EMAIL_RE.sub("[REDACTED_EMAIL]", out)

    # Multi-OS user paths & Windows UNC paths.
    out = _LINUX_VAR_HOME_RE.sub("<HOME>", out)
    out = _LINUX_HOME_RE.sub("<HOME>", out)
    out = _MACOS_USERS_RE.sub("<HOME>", out)
    out = _WIN_ESCAPED_USERS_RE.sub("<HOME>", out)
    out = _WIN_BACKSLASH_USERS_RE.sub("<HOME>", out)
    out = _WIN_SLASH_USERS_RE.sub("<HOME>", out)
    out = _WIN_ESCAPED_UNC_RE.sub("<UNC_PATH>", out)
    out = _WIN_UNC_RE.sub("<UNC_PATH>", out)

    return out


def sanitize_url(url: str | None, zotero_data_dir: Path | str | None = None) -> str:
    """Strip userinfo credentials, query parameters, and fragments from a URL."""
    if not url or not str(url).strip():
        return ""
    raw = str(url).strip()
    try:
        parsed = urllib.parse.urlsplit(raw)
        if not parsed.scheme and not parsed.netloc:
            return sanitize_text(raw, zotero_data_dir=zotero_data_dir)
        host = parsed.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        try:
            port = parsed.port
        except ValueError:
            port = None
        clean_netloc = f"{host}:{port}" if port is not None else host
        cleaned = urllib.parse.urlunsplit((parsed.scheme, clean_netloc, parsed.path, "", ""))
        return sanitize_text(cleaned, zotero_data_dir=zotero_data_dir)
    except Exception:
        return sanitize_text(raw, zotero_data_dir=zotero_data_dir)


def _read_tail_lines_binary(
    file_path: Path,
    max_lines: int,
    max_bytes: int = LOG_TAIL_SEEK_BYTES,
) -> list[str]:
    """Read up to ``max_lines`` from the end of ``file_path`` via binary EOF seek."""
    try:
        if not file_path.is_file():
            return []
        with open(file_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            file_size = f.tell()
            if file_size <= 0:
                return []
            start_pos = max(0, file_size - max_bytes)
            f.seek(start_pos, os.SEEK_SET)
            chunk = f.read(file_size - start_pos)
    except (OSError, ValueError):
        return []

    decoded = chunk.decode("utf-8", errors="replace")
    lines = decoded.splitlines()
    # Drop potentially mid-line first fragment when seeking into the middle of a large file.
    if start_pos > 0 and len(lines) > 1:
        lines = lines[1:]
    return lines[-max_lines:] if max_lines > 0 else []


def get_sanitized_log_tail(
    zotero_data_dir: Path | str | None,
    max_lines: int = DEFAULT_LOG_TAIL_LINES,
) -> list[str]:
    """Read and sanitize the tail of ``zocrates.log`` with ``zocrates.log.1`` fallback.

    Bounds line count between 50 and 500, sanitizes each line before length capping
    so partial secret prefixes cannot leak, and caps each line to 500 characters.
    Fails open (returns ``[]``) if the log directory or files do not exist.
    """
    bounded_lines = max(MIN_LOG_TAIL_LINES, min(MAX_LOG_TAIL_LINES, int(max_lines)))
    resolved_dir = _resolve_zotero_dir_readonly(zotero_data_dir)
    if resolved_dir is None:
        return []

    log_dir = resolved_dir / ZOCRATES_DATA_SUBDIR / "logs"
    primary_log = log_dir / "zocrates.log"
    rotated_log = log_dir / "zocrates.log.1"

    raw_lines = _read_tail_lines_binary(primary_log, max_lines=bounded_lines)
    if len(raw_lines) < bounded_lines:
        needed = bounded_lines - len(raw_lines)
        prev_lines = _read_tail_lines_binary(rotated_log, max_lines=needed)
        if prev_lines:
            raw_lines = (prev_lines + raw_lines)[-bounded_lines:]

    sanitized_lines: list[str] = []
    exact_secrets = _collect_exact_secrets(resolved_dir)
    path_variants = _collect_path_variants(resolved_dir)
    for raw_line in raw_lines:
        # Bound pathological multi-megabyte lines before regex pass, then sanitize before 500-char cap.
        pre_bounded = raw_line[:4096]
        clean_line = sanitize_text(
            pre_bounded,
            zotero_data_dir=resolved_dir,
            exact_secrets=exact_secrets,
            path_variants=path_variants,
        )
        if len(clean_line) > MAX_LOG_LINE_CHARS:
            clean_line = clean_line[: MAX_LOG_LINE_CHARS - 3] + "..."
        sanitized_lines.append(clean_line)

    return sanitized_lines


def extract_sanitized_config(
    zotero_data_dir: Path | str | None = None,
    raw_cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Serialize strictly allowlisted technical configuration keys and boolean key flags."""
    resolved_dir = _resolve_zotero_dir_readonly(zotero_data_dir)
    cfg = copy.deepcopy(raw_cfg) if raw_cfg is not None else _read_config_readonly(resolved_dir)

    override_models = (
        cfg.get("override_agent_models")
        if isinstance(cfg.get("override_agent_models"), dict)
        else {}
    )
    coordinator_spec = (
        override_models.get("coordinator")
        if isinstance(override_models.get("coordinator"), dict)
        else {}
    )
    subagent_thinking_default = {
        role: spec.get("thinking_level")
        for role, spec in override_models.items()
        if role != "coordinator" and isinstance(spec, dict)
    }

    normalized_lookup: dict[str, Any] = {
        "llm_provider": cfg.get("llm_provider"),
        "llm_model": cfg.get("llm_model"),
        "coordinator_thinking": (
            cfg["coordinator_thinking"]
            if "coordinator_thinking" in cfg
            else coordinator_spec.get("thinking_level")
        ),
        "subagent_thinking": (
            cfg["subagent_thinking"]
            if "subagent_thinking" in cfg
            else subagent_thinking_default
        ),
        "embedding_mode": cfg.get("embedding_mode") or cfg.get("embedding_provider"),
        "embedding_model": cfg.get("embedding_model"),
        "embedding_dimension": cfg.get("embedding_dimension"),
        "document_converter_id": cfg.get("document_converter_id"),
        "docling_mode": cfg.get("docling_mode"),
        "datalab_merge_cross_page": cfg.get("datalab_merge_cross_page"),
        "citations_enabled": cfg.get("citations_enabled"),
        "references_section_enabled": cfg.get("references_section_enabled"),
        "auto_render_mermaid_diagrams": cfg.get("auto_render_mermaid_diagrams"),
        "enable_mcp_server": cfg.get("enable_mcp_server"),
        "citation_verification_enabled": cfg.get("citation_verification_enabled"),
        "citation_verification_provider": cfg.get("citation_verification_provider"),
        "citation_verification_model": cfg.get("citation_verification_model"),
        "citation_verification_threshold": cfg.get("citation_verification_threshold"),
        "adaptive_compaction_enabled": (
            cfg["adaptive_compaction_enabled"]
            if "adaptive_compaction_enabled" in cfg
            else cfg.get("enable_adaptive_compaction")
        ),
        "session_retention_days": cfg.get("session_retention_days"),
        "session_max_count": (
            cfg["session_max_count"]
            if "session_max_count" in cfg
            else cfg.get("max_saved_sessions")
        ),
    }

    result: dict[str, Any] = {}
    for key in sorted(ALLOWED_CONFIG_KEYS):
        val = normalized_lookup.get(key)
        if isinstance(val, str):
            val = sanitize_text(val, zotero_data_dir=resolved_dir)
        elif isinstance(val, dict):
            val = {
                str(k): sanitize_text(v, zotero_data_dir=resolved_dir)
                if isinstance(v, str)
                else v
                for k, v in val.items()
            }
        result[key] = val

    raw_base_url = cfg.get("llm_base_url")
    if isinstance(raw_base_url, str) and raw_base_url.strip():
        result["llm_base_url"] = sanitize_url(raw_base_url, zotero_data_dir=resolved_dir)

    return result


def _format_context_chips(
    raw_items: list[Any] | None,
    *,
    include_item_titles: bool,
    zotero_data_dir: Path | str | None = None,
) -> list[str]:
    """Format attached context chips while honoring ``include_item_titles``."""
    if not raw_items or not isinstance(raw_items, list):
        return []

    formatted: list[str] = []
    for item in raw_items[:25]:
        if not isinstance(item, dict):
            continue
        raw_key = (
            item.get("key")
            or item.get("item_key")
            or item.get("itemKey")
            or "unknown"
        )
        safe_key = sanitize_text(str(raw_key), zotero_data_dir=zotero_data_dir)[:32]
        if include_item_titles:
            raw_title = str(item.get("title") or "Untitled")
            clean_title = sanitize_text(raw_title, zotero_data_dir=zotero_data_dir)
            formatted.append(f"[Key: {safe_key}] {clean_title}")
        else:
            formatted.append(f"[Key: {safe_key}] {OMITTED_TITLE_TEXT}")
    return formatted


def extract_shallow_turn_context(
    session: Any | None,
    *,
    invocation_id: str | None = None,
    error_message: str | None = None,
    include_prompts: bool = False,
    include_item_titles: bool = False,
    fallback_active_items: list[Any] | None = None,
    zotero_data_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Extract shallow turn metadata and tool trace without chunk payloads or thoughts."""
    session_id = str(getattr(session, "id", "") or "") if session is not None else ""
    resolved_inv_id = sanitize_text(invocation_id, zotero_data_dir=zotero_data_dir) if invocation_id else None
    clean_error = (
        sanitize_text(error_message, zotero_data_dir=zotero_data_dir)
        if error_message
        else None
    )

    user_evt = None
    asst_evts: list[Any] = []

    if session is not None:
        events = list(getattr(session, "events", []) or [])
        has_rewinds = any(
            isinstance(
                getattr(getattr(e, "actions", None), "rewind_before_invocation_id", None),
                str,
            )
            for e in events
        )
        if has_rewinds:
            with contextlib.suppress(Exception):
                events = _apply_rewinds(events)
        events = [e for e in events if not _is_compaction_event(e)]

        turns: list[tuple[Any | None, list[Any]]] = []
        cur_user = None
        cur_asst: list[Any] = []
        for evt in events:
            author = getattr(evt, "author", "assistant")
            if author == "user":
                u_txt, _, _, u_frs = extract_event_parts(evt)
                if not u_txt.strip() and u_frs:
                    if cur_user is not None:
                        cur_asst.append(evt)
                    continue
                if cur_user is not None:
                    turns.append((cur_user, cur_asst))
                cur_user = evt
                cur_asst = []
            else:
                if cur_user is not None:
                    cur_asst.append(evt)
        if cur_user is not None:
            turns.append((cur_user, cur_asst))

        if turns:
            matched_turn = None
            if invocation_id:
                for u_e, a_es in reversed(turns):
                    if u_e is not None and getattr(u_e, "invocation_id", None) == invocation_id:
                        matched_turn = (u_e, a_es)
                        break
                    if any(getattr(ae, "invocation_id", None) == invocation_id for ae in a_es):
                        matched_turn = (u_e, a_es)
                        break
            if matched_turn is None and not invocation_id:
                matched_turn = turns[-1]
            if matched_turn is not None:
                user_evt, asst_evts = matched_turn
                if not resolved_inv_id:
                    cand_inv = getattr(user_evt, "invocation_id", None) or (
                        getattr(asst_evts[-1], "invocation_id", None) if asst_evts else None
                    )
                    if cand_inv:
                        resolved_inv_id = sanitize_text(str(cand_inv), zotero_data_dir=zotero_data_dir)

    # Context chips from user event state_delta or frontend fallback.
    raw_chips = None
    if user_evt is not None:
        actions = getattr(user_evt, "actions", None)
        state_delta = getattr(actions, "state_delta", {}) if actions else {}
        if isinstance(state_delta, dict):
            raw_chips = state_delta.get("active_context_items")
    if not raw_chips and fallback_active_items:
        raw_chips = fallback_active_items

    chips = _format_context_chips(
        raw_chips,
        include_item_titles=include_item_titles,
        zotero_data_dir=zotero_data_dir,
    )

    # Shallow tool trace: tool names, sorted argument keys ONLY, status, and sanitized error.
    # Exclude isolation_scope retrieval events, thoughts, and raw tool return payloads.
    trace_entries: list[dict[str, Any]] = []
    call_index_by_id: dict[str, int] = {}

    for evt in asst_evts:
        if getattr(evt, "isolation_scope", None) is not None:
            continue
        author_label = format_author_name(getattr(evt, "author", None))
        _, _thoughts, calls, responses = extract_event_parts(evt)

        for call in calls:
            tool_name = str(call.get("name") or "")
            if not tool_name or tool_name in ("finish_task", "adk_request_input"):
                continue
            raw_args = call.get("args")
            arg_keys = (
                sorted(str(k) for k in raw_args.keys())
                if isinstance(raw_args, dict)
                else []
            )
            entry: dict[str, Any] = {
                "tool": sanitize_text(tool_name, zotero_data_dir=zotero_data_dir),
                "author": author_label,
                "arg_keys": arg_keys,
                "status": "dispatched",
                "error": None,
            }
            call_id = call.get("id")
            if isinstance(call_id, str) and call_id:
                call_index_by_id[call_id] = len(trace_entries)
            trace_entries.append(entry)

        for resp in responses:
            tool_name = str(resp.get("name") or "")
            if not tool_name or tool_name in ("finish_task", "adk_request_input"):
                continue
            payload = resp.get("response")
            status = "ok"
            err_detail: str | None = None
            if isinstance(payload, dict):
                raw_err = payload.get("error")
                raw_status = str(payload.get("status") or "").lower()
                if raw_err:
                    status = "error"
                    err_detail = sanitize_text(
                        str(raw_err)[:300], zotero_data_dir=zotero_data_dir
                    )
                elif raw_status in ("error", "failed"):
                    status = "error"
                    msg_val = payload.get("message") or payload.get("detail") or ""
                    if msg_val:
                        err_detail = sanitize_text(
                            str(msg_val)[:300], zotero_data_dir=zotero_data_dir
                        )

            call_id = resp.get("id")
            if isinstance(call_id, str) and call_id in call_index_by_id:
                target = trace_entries[call_index_by_id[call_id]]
                target["status"] = status
                if err_detail:
                    target["error"] = err_detail
            else:
                trace_entries.append(
                    {
                        "tool": sanitize_text(tool_name, zotero_data_dir=zotero_data_dir),
                        "author": author_label,
                        "arg_keys": [],
                        "status": status,
                        "error": err_detail,
                    }
                )

    # Prompt & assistant text strictly gated by include_prompts.
    if include_prompts:
        user_prompt_text = ""
        if user_evt is not None:
            raw_u_txt, _, _, _ = extract_event_parts(user_evt)
            user_prompt_text = sanitize_text(raw_u_txt, zotero_data_dir=zotero_data_dir)

        asst_response_text = ""
        for evt in reversed(asst_evts):
            if getattr(evt, "isolation_scope", None) is not None:
                continue
            raw_a_txt, _, _, _ = extract_event_parts(evt)
            if raw_a_txt.strip():
                asst_response_text = sanitize_text(raw_a_txt, zotero_data_dir=zotero_data_dir)
                break
        if not asst_response_text and clean_error:
            asst_response_text = clean_error

        prompt_section: dict[str, Any] = {
            "included": True,
            "user_prompt": user_prompt_text or None,
            "assistant_response": asst_response_text or None,
        }
    else:
        prompt_section = {
            "included": False,
            "placeholder": OMITTED_PROMPTS_TEXT,
        }

    return {
        "session_id": sanitize_text(session_id, zotero_data_dir=zotero_data_dir) or None,
        "invocation_id": resolved_inv_id,
        "error_message": clean_error,
        "context_chips": chips,
        "prompts": prompt_section,
        "execution_trace": trace_entries,
    }


def _sanitize_frontend_metadata(
    frontend_metadata: dict[str, Any] | None,
    *,
    include_prompts: bool,
    include_item_titles: bool,
    zotero_data_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Sanitize untrusted client-provided diagnostic metadata and notifications."""
    if not isinstance(frontend_metadata, dict):
        return {
            "zotero_version": "Unknown",
            "zotero_platform": "Unknown",
            "extension_version": "0.1.0",
            "window_mode": "unknown",
            "backend_connection": "Connected",
            "heartbeat_latency_ms": None,
            "notifications": [],
        }

    def _clean_short(key: str, default: str = "Unknown", max_len: int = 120) -> str:
        val = frontend_metadata.get(key)
        if val is None or val == "":
            return default
        return sanitize_text(str(val), zotero_data_dir=zotero_data_dir)[:max_len]

    raw_latency = frontend_metadata.get("heartbeat_latency_ms")
    latency_ms: float | None = None
    if isinstance(raw_latency, (int, float)) and not isinstance(raw_latency, bool) and raw_latency >= 0:
        latency_ms = round(float(raw_latency), 1)

    # Cap notifications at last 5 and scrub titles/queries when toggles are false.
    raw_notifs = frontend_metadata.get("notifications")
    clean_notifs: list[dict[str, str]] = []
    if isinstance(raw_notifs, list):
        for item in raw_notifs[:5]:
            if not isinstance(item, dict):
                continue
            n_type = sanitize_text(str(item.get("type") or "info"), zotero_data_dir=zotero_data_dir)[:24]
            n_text = sanitize_text(str(item.get("text") or ""), zotero_data_dir=zotero_data_dir)
            if not include_item_titles:
                # Scrub paper titles embedded in auto/batch converter notifications.
                n_text = re.sub(
                    r"((?:⚡\s*)?(?:Re-converting|Converting)(?:\s+new)?(?:\s+[^:]*)?:\s*)(.+?)(\s+with\s+)",
                    rf"\1{OMITTED_TITLE_TEXT}\3",
                    n_text,
                )
                n_text = re.sub(
                    r"(Indexing failed:\s*)(.+?)(\s+\()",
                    rf"\1{OMITTED_TITLE_TEXT}\3",
                    n_text,
                )
                # Scrub double-quoted note/paper titles (e.g. Saved "Note" attached directly under "Paper").
                n_text = re.sub(
                    r'"([^"\n\r]{2,})"',
                    rf'"{OMITTED_TITLE_TEXT}"',
                    n_text,
                )
            if not include_prompts and len(n_text) > 240:
                n_text = n_text[:237] + "..."
            else:
                n_text = n_text[:400]
            n_ts = sanitize_text(str(item.get("timestamp") or ""), zotero_data_dir=zotero_data_dir)[:40]
            clean_notifs.append({"type": n_type, "text": n_text, "timestamp": n_ts})

    return {
        "zotero_version": _clean_short("zotero_version", "Unknown"),
        "zotero_platform": _clean_short("zotero_platform", "Unknown"),
        "extension_version": _clean_short("extension_version", "0.1.0"),
        "window_mode": _clean_short("window_mode", "sidebar"),
        "backend_connection": _clean_short("backend_connection", "Connected"),
        "heartbeat_latency_ms": latency_ms,
        "notifications": clean_notifs,
    }


def build_diagnostic_snapshot(
    *,
    zotero_data_dir: Path | str | None = None,
    session: Any | None = None,
    session_id: str | None = None,
    invocation_id: str | None = None,
    error_message: str | None = None,
    include_prompts: bool = False,
    include_item_titles: bool = False,
    include_logs: bool = False,
    log_tail_lines: int = DEFAULT_LOG_TAIL_LINES,
    frontend_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble a sanitized diagnostic snapshot dictionary on a worker thread."""
    resolved_dir = _resolve_zotero_dir_readonly(zotero_data_dir)
    generated_at = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    # 1. Hardware & OS profile (cached, no cold subprocess probes if already cached).
    hw_dict: dict[str, Any] = {}
    docling_assessment: dict[str, Any] = {}
    with contextlib.suppress(Exception):
        hw_dict = get_system_hardware_info(force_refresh=False).to_dict()
    with contextlib.suppress(Exception):
        docling_assessment = get_docling_hardware_assessment()
        if isinstance(docling_assessment.get("reason"), str):
            docling_assessment = dict(docling_assessment)
            docling_assessment["reason"] = sanitize_text(
                docling_assessment["reason"], zotero_data_dir=resolved_dir
            )

    environment = {
        "os_platform": sanitize_text(platform.system(), zotero_data_dir=resolved_dir),
        "os_release": sanitize_text(platform.release(), zotero_data_dir=resolved_dir),
        "architecture": sanitize_text(platform.machine(), zotero_data_dir=resolved_dir),
        "python_version": sanitize_text(
            sys.version.split()[0], zotero_data_dir=resolved_dir
        ),
        "backend_version": sanitize_text(__version__, zotero_data_dir=resolved_dir),
        "backend_port": BACKEND_PORT,
        "backend_pid": os.getpid(),
        "hardware": hw_dict,
        "docling_assessment": docling_assessment,
    }

    # 2. Strict configuration allowlist + boolean API key presence flags.
    config_allowlist = extract_sanitized_config(resolved_dir)
    api_key_status = {
        "has_openrouter_key": bool(get_secure_api_key()),
        "has_datalab_key": bool(get_datalab_api_key(resolved_dir)),
        "has_openalex_key": bool(get_openalex_api_key(resolved_dir)),
    }

    # 3. Frontend context.
    client_info = _sanitize_frontend_metadata(
        frontend_metadata,
        include_prompts=include_prompts,
        include_item_titles=include_item_titles,
        zotero_data_dir=resolved_dir,
    )

    # 4. Shallow turn context & trace.
    fallback_items = (
        frontend_metadata.get("active_items")
        if isinstance(frontend_metadata, dict)
        else None
    )
    turn_context = extract_shallow_turn_context(
        session,
        invocation_id=invocation_id,
        error_message=error_message,
        include_prompts=include_prompts,
        include_item_titles=include_item_titles,
        fallback_active_items=fallback_items,
        zotero_data_dir=resolved_dir,
    )
    if not turn_context.get("session_id") and session_id:
        turn_context["session_id"] = sanitize_text(
            session_id, zotero_data_dir=resolved_dir
        )

    # Allow client-supplied prompt fallback when session had not persisted yet AND include_prompts=True.
    if (
        include_prompts
        and isinstance(frontend_metadata, dict)
        and isinstance(turn_context.get("prompts"), dict)
        and not turn_context["prompts"].get("user_prompt")
        and frontend_metadata.get("last_user_prompt")
    ):
        turn_context["prompts"]["user_prompt"] = sanitize_text(
            str(frontend_metadata["last_user_prompt"]),
            zotero_data_dir=resolved_dir,
        )

    # 5. Backend log tail (strictly opt-in).
    if include_logs:
        logs_section: dict[str, Any] = {
            "included": True,
            "lines": get_sanitized_log_tail(resolved_dir, max_lines=log_tail_lines),
        }
    else:
        logs_section = {
            "included": False,
            "placeholder": OMITTED_LOGS_TEXT,
            "lines": [],
        }

    return {
        "generated_at": generated_at,
        "toggles": {
            "include_prompts": bool(include_prompts),
            "include_item_titles": bool(include_item_titles),
            "include_logs": bool(include_logs),
        },
        "environment": environment,
        "frontend": client_info,
        "config": config_allowlist,
        "api_key_status": api_key_status,
        "turn_context": turn_context,
        "logs": logs_section,
    }


def _render_markdown_from_parts(
    data: dict[str, Any],
    *,
    log_lines: list[str] | None,
    trace_entries: list[dict[str, Any]],
    max_prompt_chars: int | None = None,
    logs_truncated: bool = False,
    trace_truncated: bool = False,
) -> str:
    """Render GitHub-flavored Markdown report with collapsible ``<details>`` sections."""
    env = data.get("environment") or {}
    fe = data.get("frontend") or {}
    cfg = data.get("config") or {}
    key_status = data.get("api_key_status") or {}
    turn = data.get("turn_context") or {}
    logs_info = data.get("logs") or {}

    hw = env.get("hardware") or {}
    docling = env.get("docling_assessment") or {}

    err_msg = turn.get("error_message") or "No specific error message attached (manual report)."
    if max_prompt_chars is not None and len(err_msg) > max_prompt_chars:
        err_msg = err_msg[:max_prompt_chars] + f"\n{TRUNCATION_MARKER}"

    latency_val = fe.get("heartbeat_latency_ms")
    latency_str = f"{latency_val} ms" if latency_val is not None else "N/A"

    vram_val = hw.get("vram_gb")
    vram_str = f"{vram_val} GB" if vram_val is not None else "N/A"
    cc_val = hw.get("compute_capability")
    cc_str = str(cc_val) if cc_val is not None else "N/A"

    lines: list[str] = [
        "## Zocrates Diagnostic & Error Report",
        "",
        f"- **Generated**: `{data.get('generated_at', 'Unknown')}`",
        f"- **Zocrates Backend Version**: `{env.get('backend_version', 'Unknown')}`",
        f"- **Zocrates Extension Version**: `{fe.get('extension_version', '0.1.0')}`",
        f"- **Zotero Version**: `{fe.get('zotero_version', 'Unknown')}` (`{fe.get('zotero_platform', 'Unknown')}`)",
        "",
        "### Error Summary",
        "```text",
        str(err_msg),
        "```",
        "",
        "### Environment & Hardware",
        f"- **OS / Architecture**: `{env.get('os_platform', 'Unknown')} {env.get('os_release', '')}` (`{env.get('architecture', 'Unknown')}`)",
        f"- **Python Runtime**: `{env.get('python_version', 'Unknown')}`",
        f"- **Window Mode**: `{fe.get('window_mode', 'sidebar')}`",
        f"- **Backend Connection**: `{fe.get('backend_connection', 'Connected')}` (Port: `{env.get('backend_port', 'Unknown')}`, PID: `{env.get('backend_pid', 'Unknown')}`, Latency: `{latency_str}`)",
        f"- **System RAM**: `{hw.get('ram_gb', 'Unknown')} GB`",
        f"- **GPU**: `{hw.get('gpu_vendor', 'cpu')}` — `{hw.get('gpu_name', 'Unknown')}` (VRAM: `{vram_str}`, Compute Capability: `{cc_str}`)",
        f"- **Docling Assessment**: `{docling.get('recommended_mode', 'N/A')}` — {docling.get('reason', 'N/A')}",
        "",
        "<details>",
        "<summary><b>Active Configuration (Sanitized Allowlist)</b></summary>",
        "",
        "```json",
        json.dumps(
            {"config": cfg, "api_key_status": key_status},
            indent=2,
            sort_keys=True,
        ),
        "```",
        "",
        "</details>",
        "",
        "<details>",
        "<summary><b>Turn & Execution Context</b></summary>",
        "",
        f"- **Session ID**: `{turn.get('session_id') or 'None'}`",
        f"- **Invocation ID**: `{turn.get('invocation_id') or 'None'}`",
    ]

    chips = turn.get("context_chips") or []
    if chips:
        lines.append("- **Context Chips**:")
        for chip in chips:
            lines.append(f"  - `{chip}`")
    else:
        lines.append("- **Context Chips**: `None`")

    prompts = turn.get("prompts") or {}
    if prompts.get("included"):
        u_prompt = str(prompts.get("user_prompt") or "<Empty>")
        a_resp = str(prompts.get("assistant_response") or "<Empty>")
        if max_prompt_chars is not None:
            if len(u_prompt) > max_prompt_chars:
                u_prompt = u_prompt[:max_prompt_chars] + f"\n{TRUNCATION_MARKER}"
            if len(a_resp) > max_prompt_chars:
                a_resp = a_resp[:max_prompt_chars] + f"\n{TRUNCATION_MARKER}"
        lines.extend(
            [
                "",
                "**User Prompt**:",
                "```text",
                u_prompt,
                "```",
                "",
                "**Assistant / Error Output**:",
                "```text",
                a_resp,
                "```",
            ]
        )
    else:
        lines.append(
            f"- **Prompt & Response**: `{prompts.get('placeholder', OMITTED_PROMPTS_TEXT)}`"
        )

    lines.append("")
    if trace_entries or trace_truncated:
        lines.append("**Shallow Tool Execution Trace**:")
        for step in trace_entries:
            keys_str = ", ".join(step.get("arg_keys") or [])
            err_suffix = f" — error: `{step['error']}`" if step.get("error") else ""
            lines.append(
                f"- `{step.get('tool', 'tool')}` (author: `{step.get('author', 'Agent')}`, "
                f"arg_keys: `[{keys_str}]`) -> `{step.get('status', 'ok')}`{err_suffix}"
            )
        if trace_truncated:
            lines.append(f"- `{TRUNCATION_MARKER}`")
    else:
        lines.append("- **Shallow Tool Execution Trace**: `None recorded`")

    lines.extend(["", "</details>", ""])

    notifs = fe.get("notifications") or []
    lines.extend(
        [
            "<details>",
            "<summary><b>Frontend Notifications (Last 5)</b></summary>",
            "",
        ]
    )
    if notifs:
        for n in notifs[:5]:
            ts_prefix = f"[{n['timestamp']}] " if n.get("timestamp") else ""
            lines.append(f"- `{n.get('type', 'info')}`: {ts_prefix}{n.get('text', '')}")
    else:
        lines.append("_No recent notifications._")
    lines.extend(["", "</details>", ""])

    lines.extend(
        [
            "<details>",
            "<summary><b>Backend Log Tail</b></summary>",
            "",
            "```text",
        ]
    )
    if logs_info.get("included"):
        if logs_truncated:
            lines.append(TRUNCATION_MARKER)
        if log_lines:
            lines.extend(log_lines)
        elif not logs_truncated:
            lines.append("<No log entries found in zocrates.log>")
    else:
        lines.append(str(logs_info.get("placeholder") or OMITTED_LOGS_TEXT))

    lines.extend(["```", "", "</details>", ""])
    return "\n".join(lines)


def format_markdown_report(data: dict[str, Any]) -> str:
    """Format diagnostic snapshot into GitHub Markdown strictly capped at <= 50 KB."""
    logs_info = data.get("logs") or {}
    orig_log_lines = list(logs_info.get("lines") or [])
    turn = data.get("turn_context") or {}
    orig_trace = list(turn.get("execution_trace") or [])

    rendered = _render_markdown_from_parts(
        data,
        log_lines=orig_log_lines,
        trace_entries=orig_trace,
    )
    if len(rendered.encode("utf-8")) <= MAX_REPORT_BYTES:
        return rendered

    # Step 1: Progressively trim older log lines first (preserving most recent tail).
    cur_logs = orig_log_lines
    logs_truncated = False
    if cur_logs:
        logs_truncated = True
        low, high = 0, len(cur_logs)
        best_count = 0
        while low <= high:
            mid = (low + high) // 2
            tail_slice = cur_logs[-mid:] if mid > 0 else []
            candidate = _render_markdown_from_parts(
                data,
                log_lines=tail_slice,
                trace_entries=orig_trace,
                logs_truncated=True,
            )
            if len(candidate.encode("utf-8")) <= MAX_REPORT_BYTES:
                best_count = mid
                low = mid + 1
            else:
                high = mid - 1
        cur_logs = cur_logs[-best_count:] if best_count > 0 else []
        rendered = _render_markdown_from_parts(
            data,
            log_lines=cur_logs,
            trace_entries=orig_trace,
            logs_truncated=True,
        )
        if len(rendered.encode("utf-8")) <= MAX_REPORT_BYTES:
            return rendered

    # Step 2: Trim execution trace entries if still exceeding 50 KB.
    cur_trace = orig_trace
    trace_truncated = False
    if cur_trace:
        trace_truncated = True
        for keep in (25, 10, 5, 0):
            if keep >= len(cur_trace):
                continue
            cur_trace = cur_trace[-keep:] if keep > 0 else []
            rendered = _render_markdown_from_parts(
                data,
                log_lines=cur_logs,
                trace_entries=cur_trace,
                logs_truncated=logs_truncated,
                trace_truncated=True,
            )
            if len(rendered.encode("utf-8")) <= MAX_REPORT_BYTES:
                return rendered

    # Step 3: Clamp prompt/response/error text lengths.
    for cap in (8000, 3000, 1000, 300):
        rendered = _render_markdown_from_parts(
            data,
            log_lines=cur_logs,
            trace_entries=cur_trace,
            max_prompt_chars=cap,
            logs_truncated=logs_truncated,
            trace_truncated=trace_truncated,
        )
        if len(rendered.encode("utf-8")) <= MAX_REPORT_BYTES:
            return rendered

    # Step 4: Unconditional UTF-8 byte ceiling guard.
    suffix = f"\n{TRUNCATION_MARKER}\n"
    suffix_bytes = suffix.encode("utf-8")
    encoded = rendered.encode("utf-8")
    prefix = encoded[: MAX_REPORT_BYTES - len(suffix_bytes)].decode(
        "utf-8", errors="ignore"
    )
    return prefix + suffix


def build_diagnostic_report_bundle(
    *,
    zotero_data_dir: Path | str | None = None,
    session: Any | None = None,
    session_id: str | None = None,
    invocation_id: str | None = None,
    error_message: str | None = None,
    include_prompts: bool = False,
    include_item_titles: bool = False,
    include_logs: bool = False,
    log_tail_lines: int = DEFAULT_LOG_TAIL_LINES,
    frontend_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build both the structured diagnostic snapshot and the 50 KB-capped Markdown report."""
    snapshot = build_diagnostic_snapshot(
        zotero_data_dir=zotero_data_dir,
        session=session,
        session_id=session_id,
        invocation_id=invocation_id,
        error_message=error_message,
        include_prompts=include_prompts,
        include_item_titles=include_item_titles,
        include_logs=include_logs,
        log_tail_lines=log_tail_lines,
        frontend_metadata=frontend_metadata,
    )
    markdown = format_markdown_report(snapshot)
    return {
        "status": "success",
        "markdown": markdown,
        "report": snapshot,
    }
