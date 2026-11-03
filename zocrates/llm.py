"""
Central LLM factory, OpenRouter model list fetcher, and connection test helpers for Zocrates.
"""

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import litellm
import requests
from google.adk.models import BaseLlm
from google.adk.models.lite_llm import LiteLlm

from zocrates.config import (
    DEFAULT_LLM_MAX_TOKENS,
    DEFAULT_LLM_MODEL,
    DEFAULT_LLM_NUM_RETRIES,
    DEFAULT_LLM_TEMPERATURE,
    DEFAULT_OLLAMA_NUM_CTX,
    PROVIDER_DEFAULT_MODELS,
    get_ollama_num_ctx,
    get_secure_api_key,
    get_zocrates_data_dir,
    load_zocrates_config,
)

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 86400  # 24 hours default expiration

_OPENROUTER_LLM_CACHE: dict = {"timestamp": 0, "data": []}
_OPENROUTER_EMBED_CACHE: dict = {"timestamp": 0, "data": []}
_OPENROUTER_DECISION_CACHE: dict = {"timestamp": 0.0, "data": []}


def format_llm_exception(
    e: Exception,
    provider: str = "openrouter_llm",
    base_url: str | None = None,
) -> str:
    """Format low-level litellm/httpx connection and API errors into actionable messages.

    Args:
        e: Caught exception.
        provider: Provider identifier ('openrouter_llm', 'ollama', 'vllm', 'openai_compatible').
        base_url: Base URL configured for local providers.

    Returns:
        User-friendly error message string.
    """
    err_str = str(e)
    err_repr = repr(e)
    type_name = type(e).__name__

    is_conn_error = (
        isinstance(e, (ConnectionError, ConnectionRefusedError))
        or "ConnectError" in type_name
        or "APIConnectionError" in type_name
        or "ServiceUnavailableError" in type_name
        or any(
            term in err_str or term in err_repr
            for term in (
                "ConnectError",
                "ConnectionRefused",
                "APIConnectionError",
                "ServiceUnavailableError",
                "Connection refused",
                "Failed to establish a new connection",
                "Failed to connect",
            )
        )
    )

    if is_conn_error:
        if provider == "ollama":
            url = base_url or "http://localhost:11434"
            return (
                f"Unable to connect to Ollama server at {url}. "
                "Please verify Ollama is installed and running (`ollama serve`)."
            )
        elif provider in ("vllm", "openai_compatible"):
            url = base_url or "http://localhost:8000/v1"
            return (
                f"Unable to connect to local server at {url}. "
                "Please verify vLLM or custom OpenAI server is running with tool choice enabled (`--enable-auto-tool-choice --tool-call-parser hermes`)."
            )
        elif provider in ("openrouter", "openrouter_llm"):
            return (
                "Unable to connect to OpenRouter API. "
                "Please check your internet connection or verify your OPENROUTER_API_KEY."
            )

    if (
        "AuthenticationError" in type_name
        or "401" in err_str
        or "Unauthorized" in err_str
        or "invalid_api_key" in err_str.lower()
    ):
        return "Authentication failed (401). Please check your API key in Zocrates settings."

    return err_str


def _format_base_url(base_url: str, provider: str) -> str:
    """Format and normalize base URL for local providers."""
    url = (base_url or "").strip()
    if not url:
        return url
    if not url.lower().startswith(("http://", "https://")):
        url = f"http://{url}"
    else:
        scheme, sep, rest = url.partition("://")
        url = f"{scheme.lower()}{sep}{rest}"
    url = url.rstrip("/")
    if provider == "ollama":
        if url.endswith("/v1"):
            url = url[:-3].rstrip("/")
    elif provider in ("vllm", "openai_compatible"):
        parsed = urlsplit(url)
        if not parsed.path or parsed.path == "/":
            url = f"{url}/v1"
    return url


def _resolve_model_name(model_slug: str, provider: str) -> str:
    """Resolve a raw model slug into a provider-prefixed LiteLLM model identifier.

    Handles provider prefixes and prevents cross-provider slug leakage (e.g. Clash #5).
    """
    model_name = model_slug.strip() if model_slug else ""
    if not model_name:
        model_name = PROVIDER_DEFAULT_MODELS.get(provider, DEFAULT_LLM_MODEL)

    if provider == "ollama":
        if model_name.startswith("ollama/"):
            stripped = model_name[len("ollama/"):]
            return f"ollama_chat/{stripped}"
        if model_name.startswith("ollama_chat/"):
            return model_name
        if "/" in model_name:
            remote_prefixes = (
                "openrouter/",
                "google/",
                "anthropic/",
                "meta-llama/",
                "deepseek/",
                "mistralai/",
                "qwen/",
            )
            is_remote_prefix = any(model_name.startswith(p) for p in remote_prefixes)
            is_hf = model_name.startswith("hf.co/") or model_name.startswith("huggingface.co/")
            if is_remote_prefix and not is_hf:
                default_ollama = PROVIDER_DEFAULT_MODELS.get("ollama", "llama3.1:8b")
                logger.warning(
                    "Model '%s' appears to be a remote provider model but provider is ollama; falling back to default '%s'",
                    model_name,
                    default_ollama,
                )
                if default_ollama.startswith("ollama_chat/"):
                    return default_ollama
                if default_ollama.startswith("ollama/"):
                    return f"ollama_chat/{default_ollama[len('ollama/'):]}"
                return f"ollama_chat/{default_ollama}"
            else:
                return f"ollama_chat/{model_name}"
        return f"ollama_chat/{model_name}"

    elif provider in ("vllm", "openai_compatible"):
        if model_name.startswith("openai/"):
            return model_name
        return f"openai/{model_name}"

    elif provider in ("openrouter", "openrouter_llm"):
        if model_name.startswith("openrouter/"):
            return model_name
        return f"openrouter/{model_name}"

    return model_name


def build_agent_llm(
    agent_cfg: dict[str, Any],
    global_cfg: dict[str, Any] | None = None,
    zotero_data_dir: Path | str | None = None,
) -> BaseLlm:
    """Build a dedicated LiteLlm instance for an agent role.

    Resolves configuration according to precedence:
      agent_cfg > global_cfg > defaults

    Args:
        agent_cfg: Role-specific override dict (e.g. from override_agent_models).
        global_cfg: Optional global configuration dictionary (loaded if None).
        zotero_data_dir: Optional Zotero data directory path.

    Returns:
        Configured ADK BaseLlm (LiteLlm) instance.
    """
    if global_cfg is None:
        global_cfg = load_zocrates_config(zotero_data_dir)

    raw_provider = agent_cfg.get("provider")
    raw_model = (
        agent_cfg.get("model")
        or global_cfg.get("llm_model")
        or PROVIDER_DEFAULT_MODELS.get(raw_provider or global_cfg.get("llm_provider", "openrouter_llm"), DEFAULT_LLM_MODEL)
    )

    if raw_provider:
        provider = raw_provider
    elif raw_model.startswith("ollama/") or raw_model.startswith("ollama_chat/"):
        provider = "ollama"
    elif raw_model.startswith("openai/"):
        provider = "vllm"
    elif raw_model.startswith("openrouter/"):
        provider = "openrouter_llm"
    else:
        provider = global_cfg.get("llm_provider") or "openrouter_llm"

    model_name = _resolve_model_name(raw_model, provider)

    kwargs: dict[str, Any] = {}

    # 1. OpenRouter (Remote LLM)
    if provider in ("openrouter", "openrouter_llm"):
        key = (
            agent_cfg.get("api_key")
            or get_secure_api_key("openrouter_api_key", ("OPENROUTER_API_KEY", "OPEN_ROUTER_API_KEY"))
        )
        if key:
            kwargs["api_key"] = key
        kwargs["extra_headers"] = {
            # "HTTP-Referer": "https://github.com/alexplaka/Zocrates",
            "X-OpenRouter-Title": "Zocrates",
        }

    # 2. Local Ollama
    elif provider == "ollama":
        raw_url = (
            agent_cfg.get("base_url")
            or global_cfg.get("llm_base_url")
            or "http://localhost:11434"
        )
        kwargs["api_base"] = _format_base_url(raw_url, "ollama")
        extra_opts = dict(global_cfg.get("llm_extra_options") or {})
        extra_opts.update(agent_cfg.get("extra_options") or {})
        effective_num_ctx = extra_opts.get("num_ctx") or get_ollama_num_ctx(cfg=global_cfg)
        try:
            kwargs["num_ctx"] = int(effective_num_ctx)
        except (TypeError, ValueError):
            kwargs["num_ctx"] = DEFAULT_OLLAMA_NUM_CTX
        other_opts = {k: v for k, v in extra_opts.items() if k != "num_ctx"}
        if other_opts:
            kwargs["extra_body"] = {"options": other_opts}

    # 3. Local vLLM / OpenAI-compatible
    elif provider in ("vllm", "openai_compatible"):
        default_url = "http://localhost:8000/v1"
        raw_url = (
            agent_cfg.get("base_url")
            or global_cfg.get("llm_base_url")
            or default_url
        )
        kwargs["api_base"] = _format_base_url(raw_url, provider)
        kwargs["api_key"] = (
            agent_cfg.get("api_key")
            or get_secure_api_key("llm_api_key", ("LLM_API_KEY",))
            or global_cfg.get("llm_api_key")
            or "dummy"
        )

    # Temperature (opt-in; None omitted)
    if "temperature" in agent_cfg and agent_cfg["temperature"] is not None:
        kwargs["temperature"] = float(agent_cfg["temperature"])
    elif (temp := global_cfg.get("llm_temperature")) is not None:
        kwargs["temperature"] = float(temp)

    # Max tokens
    if "max_tokens" in agent_cfg and agent_cfg["max_tokens"] is not None:
        kwargs["max_tokens"] = int(agent_cfg["max_tokens"])
    else:
        if (max_tok := global_cfg.get("llm_max_tokens")) is not None:
            kwargs["max_tokens"] = int(max_tok)
        else:
            kwargs["max_tokens"] = DEFAULT_LLM_MAX_TOKENS

        if provider in ("vllm", "openai_compatible") and kwargs.get("max_tokens") == 16384:
            kwargs["max_tokens"] = 4096

    if provider in ("ollama", "vllm", "openai_compatible"):
        kwargs["drop_params"] = True

    # Retries
    if "num_retries" in agent_cfg and agent_cfg["num_retries"] is not None:
        kwargs["num_retries"] = int(agent_cfg["num_retries"])
    elif (num_retries := global_cfg.get("llm_num_retries")) is not None:
        kwargs["num_retries"] = int(num_retries)
    else:
        kwargs["num_retries"] = DEFAULT_LLM_NUM_RETRIES

    return LiteLlm(model=model_name, **kwargs)


def get_llm_model(zotero_data_dir: Path | str | None = None) -> BaseLlm:
    """Instantiate the active ADK BaseLlm instance based on Zocrates configuration.

    Args:
        zotero_data_dir: Optional path to Zotero data directory.

    Returns:
        Configured ADK BaseLlm (LiteLlm) instance.
    """
    return build_agent_llm({}, zotero_data_dir=zotero_data_dir)


def _get_cache_file_path(target_type: str = "llm", zotero_data_dir: Path | str | None = None) -> Path:
    """Return the disk cache file path for OpenRouter models.

    Args:
        target_type: Type of model cache ('llm' or 'embedding').
        zotero_data_dir: Optional custom Zotero data directory path.

    Returns:
        Path to the JSON cache file.
    """
    data_dir = get_zocrates_data_dir(zotero_data_dir) / "openrouter_models_cache"
    data_dir.mkdir(parents=True, exist_ok=True)
    if target_type == "embedding":
        filename = "embedding_models.json"
    elif target_type == "decision":
        filename = "decision_models.json"
    else:
        filename = "llm_models.json"
    return data_dir / filename


def _sync_fetch_openrouter_models(key: str, target_type: str = "llm") -> list[dict]:
    """Fetch OpenRouter models catalog synchronously via HTTP GET.

    Args:
        key: OpenRouter API key for authentication.
        target_type: Type of model catalog to retrieve ('llm' or 'embedding').

    Returns:
        List of raw model dictionaries from the OpenRouter API response.
    """
    headers = {
        "Authorization": f"Bearer {key}",
        "HTTP-Referer": "https://github.com/alexplaka/Zocrates",
        "X-OpenRouter-Title": "Zocrates",
    }
    if target_type == "embedding":
        endpoint = "https://openrouter.ai/api/v1/embeddings/models"
    elif target_type == "decision":
        endpoint = "https://openrouter.ai/api/v1/models?output_modalities=decisions"
    else:
        endpoint = "https://openrouter.ai/api/v1/models"
    response = requests.get(endpoint, headers=headers, timeout=10)
    response.raise_for_status()
    return response.json().get("data", [])


async def fetch_and_cache_openrouter_models(
        target_type: str = "llm",
        force_refresh: bool = False,
        zotero_data_dir: Path | str | None = None,
) -> list[dict]:
    """Fetch OpenRouter model list (LLM or Embedding), update memory & disk cache, and return full raw data.

    Args:
        target_type: 'llm' or 'embedding'.
        force_refresh: If True, bypass cache and perform live network request.
        zotero_data_dir: Optional Zotero data directory path.

    Returns:
        List of raw model dicts from OpenRouter API.
    """
    global _OPENROUTER_LLM_CACHE, _OPENROUTER_EMBED_CACHE, _OPENROUTER_DECISION_CACHE
    if target_type == "embedding":
        cache_dict = _OPENROUTER_EMBED_CACHE
    elif target_type == "decision":
        cache_dict = _OPENROUTER_DECISION_CACHE
    else:
        cache_dict = _OPENROUTER_LLM_CACHE

    now = time.time()

    # 1. Use memory cache if fresh (< 24 hours) and not forced
    if not force_refresh and cache_dict["data"] and (now - cache_dict["timestamp"] < CACHE_TTL_SECONDS):
        return cache_dict["data"]

    # 2. Try disk cache if not forcing refresh
    cache_file = None
    try:
        cache_file = _get_cache_file_path(target_type, zotero_data_dir)
        if not force_refresh and cache_file.exists():
            with open(cache_file, "r", encoding="utf-8") as f:
                cached = json.load(f)
                if now - cached.get("timestamp", 0) < CACHE_TTL_SECONDS:
                    cache_dict.clear()
                    cache_dict.update(cached)
                    return cached.get("data", [])
    except Exception as e:
        logger.debug("Disk cache unavailable (%s): %s", target_type, e)

    # 3. Network fetch requires API key
    key = get_secure_api_key("openrouter_api_key", ("OPENROUTER_API_KEY", "OPEN_ROUTER_API_KEY"))
    if not key:
        logger.info("No OPENROUTER_API_KEY configured; skipping network model fetch.")
        return cache_dict.get("data", [])

    # 3. Perform network fetch off the main event loop
    try:
        data = await asyncio.to_thread(_sync_fetch_openrouter_models, key, target_type)
        updated_cache = {"timestamp": now, "data": data}
        cache_dict.clear()
        cache_dict.update(updated_cache)

        # Write full raw endpoint response data to disk cache if available
        if cache_file is not None:
            try:
                with open(cache_file, "w", encoding="utf-8") as f:
                    json.dump(updated_cache, f, indent=2)
            except Exception as disk_err:
                logger.debug("Failed to write disk cache (%s): %s", target_type, disk_err)

        # Invalidate in-memory rate cache so fresh pricing takes effect immediately
        if target_type == "llm":
            try:
                from zocrates.utils.cost_calculator import clear_rate_cache
                clear_rate_cache()
            except Exception:
                pass

        return data
    except Exception as e:
        logger.error("Failed to fetch OpenRouter models (%s): %s", target_type, e)
        return cache_dict.get("data", [])


async def get_openrouter_models(
        free_only: bool = False,
        modality: str | None = None,
        min_context: int = 0,
        target_type: str = "llm",
        force_refresh: bool = False,
        zotero_data_dir: Path | str | None = None,
) -> list[dict]:
    """Filter session-cached OpenRouter models for LLMs or Cloud Embeddings.

    Filters out unneeded modalities: audio, video, rerank, speech, and transcription.
    """
    raw_models = await fetch_and_cache_openrouter_models(
        target_type=target_type,
        force_refresh=force_refresh,
        zotero_data_dir=zotero_data_dir,
    )

    if target_type == "decision":
        decision_models = []
        for m in raw_models:
            model_id = m.get("id", "")
            if not model_id:
                continue
            arch = m.get("architecture") or {}
            out_mods = arch.get("output_modalities") or []
            if out_mods and "decisions" not in out_mods:
                continue
            model_name = m.get("name") or model_id
            context_len = m.get("context_length", 0)
            if min_context and context_len < min_context:
                continue
            description = m.get("description", "")
            pricing = m.get("pricing", {}) or {}
            try:
                p_prompt = float(pricing.get("prompt", -1))
                p_comp = float(pricing.get("completion", -1))
                is_free = model_id.endswith(":free") or (p_prompt == 0.0 and p_comp == 0.0)
            except (ValueError, TypeError):
                is_free = model_id.endswith(":free")
            if free_only and not is_free:
                continue
            decision_models.append(
                {
                    "id": model_id,
                    "name": model_name,
                    "context_length": context_len,
                    "description": description,
                    "pricing": pricing,
                    "architecture": arch,
                    "is_free": is_free,
                }
            )
        decision_models.sort(key=lambda x: x["name"].lower())
        return decision_models

    filtered = []

    EXCLUDED_MODALITIES = {"audio", "video", "speech", "transcription", "rerank"}

    for m in raw_models:
        model_id = m.get("id", "")
        model_name = m.get("name", model_id)
        context_len = m.get("context_length", 0)
        arch = m.get("architecture", {}) or {}
        input_mods = m.get("input_modalities") or arch.get("input_modalities") or []
        output_mods = m.get("output_modalities") or arch.get("output_modalities") or []
        pricing = m.get("pricing", {}) or {}

        if not input_mods or not output_mods:
            mod_str = arch.get("modality") or m.get("modality") or ""
            if "->" in mod_str:
                in_part, out_part = mod_str.split("->", 1)
                if not input_mods:
                    input_mods = [x.strip() for x in in_part.split("+") if x.strip()]
                if not output_mods:
                    output_mods = [x.strip() for x in out_part.split("+") if x.strip()]
            elif mod_str and not input_mods:
                input_mods = [x.strip() for x in mod_str.split("+") if x.strip()]

        if not input_mods:
            input_mods = ["text"]
        if not output_mods:
            output_mods = ["text"]

        # Modality checks
        if target_type == "embedding":
            if "text" not in input_mods:
                continue
        else:
            if "text" not in input_mods or "text" not in output_mods:
                continue

        id_lower = model_id.lower()
        # Strict Batch model exclusion
        if (
            "batch" in id_lower
            or "batch" in model_name.lower()
            or id_lower.endswith(":batch")
            or "(batch)" in model_name.lower()
        ):
            continue

        # Exclude non-LLM models (rerank, whisper, tts)
        if any(
            term in id_lower
            for term in (
                "rerank",
                "whisper",
                "tts",
            )
        ):
            continue

        def _is_zero_price(val: Any) -> bool:
            """Check whether a pricing value is zero.

            Args:
                val: Pricing scalar (string, float, int) to test.

            Returns:
                True if value evaluates to numeric 0.0, False otherwise.
            """
            try:
                return float(val) == 0.0
            except (ValueError, TypeError):
                return False

        is_free = model_id.endswith(":free") or (
                _is_zero_price(pricing.get("prompt")) and _is_zero_price(pricing.get("completion"))
        )
        if free_only and not is_free:
            continue
        if modality and modality not in input_mods:
            continue
        if min_context and context_len < min_context:
            continue

        formatted_id = f"openrouter/{model_id}" if not model_id.startswith("openrouter/") else model_id

        filtered.append(
            {
                "id": formatted_id,
                "name": model_name,
                "context_length": context_len,
                "input_modalities": input_mods,
                "output_modalities": output_mods,
                "architecture": arch,
                "pricing": pricing,
                "is_free": is_free,
            }
        )
    filtered.sort(key=lambda x: x["name"].lower())
    return filtered


async def test_llm_connection(
    provider: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
    openrouter_api_key: str | None = None,
    zotero_data_dir: Path | str | None = None,
) -> dict:
    """Perform a lightweight test completion request to verify LLM connectivity.

    Supports testing unsaved form parameters or defaults to saved config.
    """
    cfg = load_zocrates_config(zotero_data_dir)
    provider = provider or cfg.get("llm_provider", "openrouter_llm")
    raw_model = model or cfg.get("llm_model") or PROVIDER_DEFAULT_MODELS.get(provider, DEFAULT_LLM_MODEL)
    model_name = _resolve_model_name(raw_model, provider)

    kwargs: Any = {"max_tokens": 5}

    if provider in ("openrouter", "openrouter_llm"):
        key = openrouter_api_key or get_secure_api_key("openrouter_api_key", ("OPENROUTER_API_KEY", "OPEN_ROUTER_API_KEY"))
        if not key:
            return {
                "ok": False,
                "status": "error",
                "message": "OPENROUTER_API_KEY is not configured.",
                "model": model_name,
            }
        kwargs["api_key"] = key
        kwargs["extra_headers"] = {
            # "HTTP-Referer": "https://github.com/alexplaka/Zocrates",
            "X-OpenRouter-Title": "Zocrates",
        }

    elif provider == "ollama":
        raw_url = base_url or cfg.get("llm_base_url") or "http://localhost:11434"
        kwargs["api_base"] = _format_base_url(raw_url, "ollama")
        kwargs["num_ctx"] = get_ollama_num_ctx(cfg=cfg)
        kwargs["drop_params"] = True

    elif provider in ("vllm", "openai_compatible"):
        raw_url = base_url or cfg.get("llm_base_url") or "http://localhost:8000/v1"
        kwargs["api_base"] = _format_base_url(raw_url, provider)
        kwargs["api_key"] = (
            get_secure_api_key("llm_api_key", ("LLM_API_KEY",))
            or cfg.get("llm_api_key")
            or "dummy"
        )
        kwargs["drop_params"] = True

    try:
        messages = [{"role": "user", "content": "Ping"}]
        response = await asyncio.to_thread(
            litellm.completion,
            model=model_name,
            messages=messages,
            **kwargs,
        )
        content = "OK"
        if response and getattr(response, "choices", None):
            choices = response.choices
            if isinstance(choices, (list, tuple)) and len(choices) > 0:
                first = choices[0]
                if isinstance(first, dict):
                    msg = first.get("message", {})
                    content = msg.get("content", "OK") if isinstance(msg, dict) else getattr(msg, "content", "OK")
                elif hasattr(first, "message"):
                    msg = getattr(first, "message", None)
                    if msg is not None:
                        content = getattr(msg, "content", "OK") if hasattr(msg, "content") else (msg.get("content", "OK") if isinstance(msg, dict) else "OK")
        if not content:
            content = "OK"

        # Tool capability probe for local endpoints (Ollama, vLLM, OpenAI-compatible)
        if provider in ("ollama", "vllm", "openai_compatible"):
            try:
                dummy_tools = [
                    {
                        "type": "function",
                        "function": {
                            "name": "ping_tool",
                            "description": "Diagnostic probe verifying function calling / tool support.",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "query": {
                                        "type": "string",
                                        "description": "Probe query parameter.",
                                    }
                                },
                                "required": ["query"],
                            },
                        },
                    }
                ]
                probe_kwargs = dict(kwargs)
                probe_kwargs["tools"] = dummy_tools
                probe_kwargs["tool_choice"] = "auto"
                await asyncio.to_thread(
                    litellm.completion,
                    model=model_name,
                    messages=[{"role": "user", "content": "Ping"}],
                    **probe_kwargs,
                )
            except Exception as tool_exc:
                logger.warning("Tool capability probe failed for %s (%s): %s", model_name, provider, tool_exc)
                if provider in ("vllm", "openai_compatible"):
                    return {
                        "ok": False,
                        "status": "error",
                        "message": (
                            f"Tool calling probe failed: {tool_exc}. "
                            "Please verify your local server is running with tool calling enabled "
                            "(for vLLM: --enable-auto-tool-choice --tool-call-parser hermes)."
                        ),
                        "model": model_name,
                    }
                else:
                    return {
                        "ok": False,
                        "status": "error",
                        "message": f"Ollama tool calling probe failed: {tool_exc}",
                        "model": model_name,
                    }

        return {
            "ok": True,
            "status": "ok",
            "message": f"Connection successful! Response: {str(content)[:30]}",
            "model": model_name,
        }
    except Exception as e:
        logger.error("LLM connection test failed for %s: %s", model_name, e)
        base_url = kwargs.get("api_base")
        user_msg = format_llm_exception(e, provider=provider, base_url=base_url)
        return {
            "ok": False,
            "status": "error",
            "message": user_msg,
            "model": model_name,
        }


async def test_embedding_connection(
    provider: str | None = None,
    model: str | None = None,
    openrouter_api_key: str | None = None,
    zotero_data_dir: Path | str | None = None,
) -> dict:
    """Perform a lightweight test embedding request to verify embedding connectivity."""
    from zocrates.rag.embeddings import OpenRouterEmbedder

    cfg = load_zocrates_config(zotero_data_dir)
    provider = provider or cfg.get("embedding_provider", "local")
    model_name = model or cfg.get("embedding_model", "jinaai/jina-embeddings-v5-text-nano")

    if provider == "local":
        return {
            "ok": True,
            "status": "ok",
            "message": "Local Jina v5 Nano embedder ready",
            "model": model_name,
            "dimension": 768,
        }

    key = openrouter_api_key or get_secure_api_key(
        "openrouter_api_key", ("OPENROUTER_API_KEY", "OPEN_ROUTER_API_KEY")
    )
    if not key:
        return {
            "ok": False,
            "status": "error",
            "message": "OPENROUTER_API_KEY is not configured.",
            "model": model_name,
        }

    try:
        def _run_embedding() -> int:
            """Embed a test string and return embedding dimension.

            Returns:
                Integer dimension of the test embedding vector.
            """
            embedder = OpenRouterEmbedder(model_name=model_name, api_key=key)
            vecs = embedder(["test"])
            dim = len(vecs[0]) if vecs and len(vecs) > 0 else 0
            return dim

        dim = await asyncio.to_thread(_run_embedding)
        return {
            "ok": True,
            "status": "ok",
            "message": f"Connected! (dimension: {dim})",
            "model": model_name,
            "dimension": dim,
        }
    except Exception as e:
        logger.error("Embedding connection test failed for %s: %s", model_name, e)
        user_msg = format_llm_exception(e, provider="openrouter_llm")
        return {
            "ok": False,
            "status": "error",
            "message": user_msg,
            "model": model_name,
        }


def is_model_vision_capable(
    model_name: str,
    provider: str = "openrouter_llm",
    zotero_data_dir: Path | str | None = None,
) -> bool:
    """Determine whether an LLM model identifier natively supports vision inputs.

    Checks:
    1. Tier 1: Authoritative OpenRouter catalog lookup (in-memory or disk cache).
       If found, modality metadata determines capability authoritatively.
    2. Tier 2: Structural vision indicators (keywords 'vision', 'multimodal', 'vlm', and '-vl' suffixes).
    3. Tier 3: Multimodal model families (Gemini, Claude 3+, GPT-4+, OpenAI o-series, GLM vision,
       Grok, Amazon Nova, Qwen flash, Pixtral, LLaVA, Omni, Molmo, InternVL, MiniCPM, CogVLM,
       Florence, Chameleon, Aria, PaliGemma).
    4. Tier 4: LiteLLM capability fallback (only for non-OpenRouter providers).
    """
    if not model_name:
        return False

    clean_model = model_name.lower().strip()
    bare_model = clean_model[len("openrouter/"):] if clean_model.startswith("openrouter/") else clean_model
    is_openrouter = clean_model.startswith("openrouter/") or (
        bool(provider) and provider.lower() in ("openrouter", "openrouter_llm")
    )

    # Tier 1: Authoritative OpenRouter Catalog Lookup
    if is_openrouter:
        target_ids = {clean_model, bare_model, f"openrouter/{bare_model}"}
        catalog_entry = None

        # Check memory cache _OPENROUTER_LLM_CACHE
        raw_models = (
            _OPENROUTER_LLM_CACHE.get("data", [])
            if isinstance(_OPENROUTER_LLM_CACHE, dict)
            else (_OPENROUTER_LLM_CACHE if isinstance(_OPENROUTER_LLM_CACHE, list) else [])
        )
        if isinstance(raw_models, list):
            for m in raw_models:
                if isinstance(m, dict):
                    mid = str(m.get("id") or "").lower().strip()
                    mid_bare = mid[len("openrouter/"):] if mid.startswith("openrouter/") else mid
                    if mid in target_ids or mid_bare in target_ids:
                        catalog_entry = m
                        break

        # If empty or model not in memory cache, read disk cache
        if catalog_entry is None:
            try:
                candidates: list[Path] = []
                try:
                    candidates.append(_get_cache_file_path("llm", zotero_data_dir))
                except Exception:
                    pass
                if zotero_data_dir:
                    p = Path(zotero_data_dir)
                    candidates.append(p / "openrouter_models_cache" / "llm_models.json")
                    candidates.append(p / "zocrates-data" / "openrouter_models_cache" / "llm_models.json")

                disk_models = []
                for cache_path in candidates:
                    if cache_path.exists():
                        with open(cache_path, "r", encoding="utf-8") as f:
                            cached_data = json.load(f)
                        disk_models = (
                            cached_data.get("data", [])
                            if isinstance(cached_data, dict)
                            else (cached_data if isinstance(cached_data, list) else [])
                        )
                        if disk_models:
                            # Populate in-memory cache if empty
                            if (
                                isinstance(_OPENROUTER_LLM_CACHE, dict)
                                and not _OPENROUTER_LLM_CACHE.get("data")
                                and isinstance(cached_data, dict)
                            ):
                                _OPENROUTER_LLM_CACHE.clear()
                                _OPENROUTER_LLM_CACHE.update(cached_data)
                            break

                if isinstance(disk_models, list):
                    for m in disk_models:
                        if isinstance(m, dict):
                            mid = str(m.get("id") or "").lower().strip()
                            mid_bare = mid[len("openrouter/"):] if mid.startswith("openrouter/") else mid
                            if mid in target_ids or mid_bare in target_ids:
                                catalog_entry = m
                                break
            except Exception as disk_err:
                logger.debug("Failed reading OpenRouter disk cache in is_model_vision_capable: %s", disk_err)

        if catalog_entry is not None:
            arch = catalog_entry.get("architecture") if isinstance(catalog_entry.get("architecture"), dict) else {}
            input_modalities = catalog_entry.get("input_modalities") or arch.get("input_modalities")
            modality = catalog_entry.get("modality") or arch.get("modality")

            has_image = False
            if isinstance(input_modalities, (list, tuple, set)):
                has_image = any("image" in str(x).lower() for x in input_modalities)
            elif isinstance(input_modalities, str):
                has_image = "image" in input_modalities.lower()

            if not has_image and modality:
                if isinstance(modality, str):
                    has_image = "image" in modality.lower()
                elif isinstance(modality, (list, tuple, set)):
                    has_image = any("image" in str(x).lower() for x in modality)

            if has_image:
                return True
            if input_modalities is not None or modality is not None:
                # Explicitly in catalog with declared modalities but lacking 'image' -> authoritative False
                return False
            # If entry has no modality metadata at all, fall through to structural heuristics

    # Local Ollama declared capabilities probe
    is_ollama = clean_model.startswith(("ollama/", "ollama_chat/")) or (
        bool(provider) and provider.lower() == "ollama"
    )
    if is_ollama:
        try:
            cfg = load_zocrates_config(zotero_data_dir)
            b_url = (cfg.get("llm_base_url") or "http://localhost:11434").rstrip("/")
            bare_tag = bare_model.split("/", 1)[-1] if "/" in bare_model else bare_model
            bare_tag_base = bare_tag.split(":", 1)[0] if ":" in bare_tag else bare_tag

            # Check /api/tags
            try:
                resp = requests.get(f"{b_url}/api/tags", timeout=0.3)
                if resp.status_code == 200:
                    data = resp.json()
                    for m in data.get("models", []):
                        m_name = str(m.get("name") or m.get("model") or "").lower()
                        m_base = m_name.split(":", 1)[0] if ":" in m_name else m_name
                        if bare_tag in (m_name, m_base) or bare_tag_base in (m_name, m_base):
                            caps = [str(c).lower() for c in m.get("capabilities", [])]
                            details = m.get("details", {})
                            family = str(details.get("family", "")).lower()
                            families = [str(f).lower() for f in details.get("families", [])]
                            if "vision" in caps or "clip" in family or any("clip" in f for f in families):
                                return True
            except Exception:
                pass

            # Check /api/show
            try:
                resp = requests.post(f"{b_url}/api/show", json={"name": bare_tag}, timeout=0.3)
                if resp.status_code == 200:
                    data = resp.json()
                    caps = [str(c).lower() for c in (data.get("capabilities") or [])] if isinstance(data.get("capabilities"), list) else []
                    details = data.get("details", {})
                    family = str(details.get("family", "")).lower()
                    families = [str(f).lower() for f in details.get("families", [])]
                    if "vision" in caps or "clip" in family or any("clip" in f for f in families):
                        return True
            except Exception:
                pass
        except Exception as e:
            logger.debug("Failed querying Ollama for vision capability: %s", e)

    # Tier 2: Structural vision indicators (affixes, keywords, tags)
    if any(kw in clean_model for kw in ("vision", "multimodal", "vlm")):
        return True

    # Generalized structural regex: match any vl tag regardless of separator boundary
    # (matches qwen2.5vl, qwen3-vl, qwen4vl, deepseek-vl2, phi4vl, etc.)
    if re.search(r"(?:[-_./]|\b|[0-9a-z])vl\d*(?:[-_.:]|$|\b)", clean_model):
        return True

    # Tier 3: Multimodal model families (version-generalized)
    multimodal_family_patterns = (
        r"gemini",                                      # All Google Gemini models
        r"claude-.*(?:[3-9]|\d{2,})",                   # Anthropic Claude 3, 4, 5, 6 and beyond
        r"gpt-(?:[4-9]|\d{2,})",                        # OpenAI GPT-4, 4o, 4.5, 5, 6 and all subsequent generations
        r"(?:^|/)(?:o[1-9]|o\d{2,})(?!-mini)(?:[-_:]|$)", # OpenAI multimodal o-series (o1, o3, o4, o6...)
        r"(?:^|[/_-])glm-.*(?:flash|v\b)",              # GLM vision models (glm-5.3-flash, glm-4.5v, glm-5v-turbo, etc.)
        r"(?:^|[/_-])grok-.*(?:vision|[4-9]|\d{2,})",   # Grok vision models (grok-vision, grok-4.7, etc.)
        r"(?:^|[/_-])nova-(?:lite|pro|premier)",        # Amazon Nova models (nova-lite, nova-pro, nova-premier)
        r"(?:^|[/_-])gemma[-_]?(?:[3-9]|\d{2,})n?(?![-_:](?:1b|270m)(?:[-_:]|$))(?:[-_:]|$)", # Gemma 3 (>=4B), Gemma 4, and beyond
        r"paligemma",                                   # Google PaliGemma family
        r"qwen.*(?:vl|flash)",                          # Qwen VL and flash vision generations
        r"phi-.*(?:vision|multimodal|vl\b)",            # Microsoft Phi vision & multimodal
        r"mistral-.*(?:pixtral|vision|vl\b)",           # Mistral Pixtral & vision
        r"deepseek-.*(?:vl|vision)",                    # DeepSeek-VL family
        r"pixtral",                                     # Mistral Pixtral
        r"llava",                                       # LLaVA family
        r"omni",                                        # Omni multimodal models (e.g., Nemotron-Nano-Omni)
        r"molmo",                                       # AllenAI Molmo
        r"internvl",                                    # InternVL family
        r"minicpm",                                     # MiniCPM-V family
        r"cogvlm",                                      # CogVLM family
        r"florence",                                    # Microsoft Florence
        r"chameleon",                                   # Meta Chameleon
        r"aria",                                        # Rhymes Aria
    )

    if any(re.search(pat, clean_model) for pat in multimodal_family_patterns):
        return True

    # Tier 4: LiteLLM fallback (only for non-OpenRouter providers)
    if not is_openrouter:
        try:
            import litellm

            bare_name = clean_model.split("/")[-1] if "/" in clean_model else clean_model
            if litellm.supports_vision(model_name) or litellm.supports_vision(bare_name):
                return True
        except Exception:
            pass

    return False

