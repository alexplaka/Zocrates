"""Agent configuration helpers and thinking/reasoning parameters for Zocrates."""

import logging
from pathlib import Path
from typing import Any
import requests
from google.genai import types

from zocrates.config import DEFAULT_LLM_NUM_RETRIES, load_zocrates_config

logger = logging.getLogger(__name__)

DEFAULT_THINKING_LEVEL: str = "high"

ROLE_DEFAULT_THINKING: dict[str, str] = {
    "coordinator": "medium",
    "library_search": "low",
    "library_synthesizer": "high",
    "online_search": "low",
    "online_synthesizer": "high",
}

# Multi-turn Table of Contents caching and injection defaults
MAX_INJECTED_TOC_ITEMS: int = 3
MAX_UNIQUE_CACHED_TOCS: int = 10

_OLLAMA_CAPABILITIES_CACHE: dict[str, dict[str, Any]] = {}


def clear_ollama_capabilities_cache() -> None:
    """Clear the cached Ollama capabilities."""
    _OLLAMA_CAPABILITIES_CACHE.clear()


def is_ollama_thinking_supported(
    model_name: str | None,
    base_url: str | None = None,
    zotero_data_dir: Path | str | None = None,
) -> bool:
    """Determine whether an Ollama model supports thinking / reasoning parameters.

    Checks:
    1. Model name heuristics (e.g. contains 'thinking', 'reasoning', 'deepseek-r1', 'qwq').
    2. Cached Ollama capabilities from previous probes.
    3. Live Ollama /api/tags or /api/show capability probe when the daemon is reachable.
    """
    if not model_name:
        return False

    raw_name = getattr(model_name, "model", None) or str(model_name)
    clean_name = raw_name.lower().strip()
    bare_name = clean_name.split("/", 1)[-1] if "/" in clean_name else clean_name
    bare_base = bare_name.split(":", 1)[0] if ":" in bare_name else bare_name

    # 1. Structural model name indicators
    thinking_indicators = ("thinking", "reasoning", "deepseek-r1", "qwq")
    if any(ind in clean_name for ind in thinking_indicators):
        return True

    # 2. Check capabilities cache
    for cand in (clean_name, bare_name, bare_base):
        if cand in _OLLAMA_CAPABILITIES_CACHE:
            return bool(_OLLAMA_CAPABILITIES_CACHE[cand].get("thinking"))

    # 3. Probe Ollama daemon if reachable
    try:
        if not base_url and zotero_data_dir:
            cfg = load_zocrates_config(zotero_data_dir)
            base_url = cfg.get("llm_base_url")
        b_url = (base_url or "http://localhost:11434").rstrip("/")

        # Check /api/tags (only if capabilities field is explicitly present in tags response)
        try:
            resp = requests.get(f"{b_url}/api/tags", timeout=0.3)
            if resp.status_code == 200:
                data = resp.json()
                for m in data.get("models", []):
                    m_name = str(m.get("name") or m.get("model") or "").lower()
                    m_base = m_name.split(":", 1)[0] if ":" in m_name else m_name
                    if "capabilities" in m and isinstance(m["capabilities"], (list, tuple)):
                        caps = [str(c).lower() for c in m["capabilities"]]
                        th_val = "thinking" in caps
                        _OLLAMA_CAPABILITIES_CACHE[m_name] = {"thinking": th_val}
                        _OLLAMA_CAPABILITIES_CACHE[m_base] = {"thinking": th_val}

                for cand in (clean_name, bare_name, bare_base):
                    if cand in _OLLAMA_CAPABILITIES_CACHE:
                        return bool(_OLLAMA_CAPABILITIES_CACHE[cand].get("thinking"))
        except Exception:
            pass

        # Check /api/show for specific model
        try:
            resp = requests.post(f"{b_url}/api/show", json={"model": bare_name, "name": bare_name}, timeout=0.3)
            if resp.status_code == 200:
                data = resp.json()
                th_val = (
                    "thinking" in data
                    or bool(data.get("capabilities", {}).get("thinking") if isinstance(data.get("capabilities"), dict) else False)
                    or "thinking" in [str(c).lower() for c in (data.get("capabilities") or []) if isinstance(data.get("capabilities"), list)]
                )
                _OLLAMA_CAPABILITIES_CACHE[bare_name] = {"thinking": th_val}
                _OLLAMA_CAPABILITIES_CACHE[bare_base] = {"thinking": th_val}
                return th_val
        except Exception:
            pass
    except Exception as e:
        logger.debug("Failed querying Ollama for thinking capability: %s", e)

    return False


def get_agent_generate_content_config(
    thinking_level: str | types.ThinkingLevel | None = DEFAULT_THINKING_LEVEL,
    include_thoughts: bool = True,
    thinking_budget: int | None = None,
    num_retries: int | None = None,
    zotero_data_dir: Path | str | None = None,
    model: str | Any | None = None,
) -> types.GenerateContentConfig | None:
    """Construct an ADK GenerateContentConfig with thinking/reasoning parameters.

    Sets ADK's native thinking_config (for Google Gemini models) as well as
    http_options.extra_body (for LiteLLM / OpenRouter / OpenAI reasoning models)
    and http_options.retry_options for baked-in network retries.

    Args:
        thinking_level: Thinking level ('minimal', 'low', 'medium', 'high', or None/disabled).
        include_thoughts: Whether thoughts are returned in the response stream.
        thinking_budget: Optional token budget for reasoning (int or None).
        num_retries: Optional maximum retry attempts for HTTP requests (defaults to llm_num_retries in config or DEFAULT_LLM_NUM_RETRIES = 4).
        zotero_data_dir: Optional root Zotero data directory for loading config.
        model: Optional model identifier or LLM instance to tailor reasoning schema and Ollama thinking gating.

    Returns:
        Configured GenerateContentConfig, or None if thinking is disabled.
    """
    if thinking_level is None:
        return None

    level_str = str(thinking_level).lower().replace("thinkinglevel.", "").strip()
    if level_str in ("none", "off", "false", "disabled", ""):
        return None

    thinking_config = types.ThinkingConfig(
        thinking_level=level_str,
        include_thoughts=include_thoughts,
        thinking_budget=thinking_budget,
    )

    # Map "minimal" to "low" for OpenAI / vLLM reasoning_effort schemas (avoiding HTTP 422)
    openai_reasoning_effort = "low" if level_str == "minimal" else level_str

    extra_body: dict[str, Any] = {
        "reasoning": {"effort": level_str},
        "reasoning_effort": openai_reasoning_effort,
    }
    if thinking_budget is not None:
        extra_body["reasoning"]["max_tokens"] = thinking_budget

    # Inspect config and model to gate think: true on Ollama models
    try:
        cfg = load_zocrates_config(zotero_data_dir)
    except Exception:
        cfg = {}

    model_name_str = None
    if model is not None:
        model_name_str = getattr(model, "model", None) or str(model)
    elif cfg:
        model_name_str = cfg.get("llm_model")

    provider = str(cfg.get("llm_provider") or "").lower()
    is_ollama = (
        (model_name_str and (
            model_name_str.startswith("ollama/")
            or model_name_str.startswith("ollama_chat/")
        ))
        or provider == "ollama"
    )

    if is_ollama:
        base_url = cfg.get("llm_base_url")
        if is_ollama_thinking_supported(model_name_str, base_url=base_url, zotero_data_dir=zotero_data_dir):
            extra_body["think"] = True
        else:
            # Model does not support thinking; do not send think: true (avoiding Ollama HTTP 400)
            extra_body.pop("think", None)

    if num_retries is None:
        try:
            val = cfg.get("llm_num_retries", DEFAULT_LLM_NUM_RETRIES)
            effective_retries = int(val) if val is not None else DEFAULT_LLM_NUM_RETRIES
        except Exception:
            effective_retries = DEFAULT_LLM_NUM_RETRIES
    else:
        effective_retries = int(num_retries)

    return types.GenerateContentConfig(
        thinking_config=thinking_config,
        http_options=types.HttpOptions(
            extra_body=extra_body,
            retry_options=types.HttpRetryOptions(attempts=effective_retries),
        ),
    )
