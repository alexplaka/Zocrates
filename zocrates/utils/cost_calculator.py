"""USD cost calculation and rate resolution engine for multi-agent chat sessions.

Provides tiered token pricing resolution:
  1. Local & Free models (:free suffix, ollama/, vllm/, local/) -> $0.000000.
  2. OpenRouter Cached Catalog (primary ground truth from live API metadata).
  3. LiteLLM cost_per_token fallback (secondary only).
  4. Safe default -> $0.000000.

Derives cost strictly from prompt, completion, and prompt-cache-read tokens
without double-counting reasoning/thoughts tokens.
"""

import json
import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# In-memory caches for O(1) rate queries
_RATE_CACHE: dict[str, tuple[float, float, float]] = {}
_CATALOG_INDEX: dict[str, tuple[float, float, float]] = {}


def clear_rate_cache() -> None:
    """Clear in-memory rate caches (useful for unit tests or after catalog refresh)."""
    global _RATE_CACHE, _CATALOG_INDEX
    _RATE_CACHE.clear()
    _CATALOG_INDEX.clear()


def normalize_agent_role(raw_author: str | None) -> str | None:
    """Normalize agent author string to standardized role name, or None if not an LLM agent.

    Returns:
      'coordinator', 'library_search', 'library_synthesizer', 'online_search',
      'online_synthesizer', 'user', or None for workflow nodes.
    """
    if not raw_author:
        return "coordinator"
    if raw_author.lower() in ("user", "human"):
        return "user"
    s = raw_author.strip()
    if s.startswith("Zocrates/"):
        s = s[9:].strip()
    s = re.sub(r"sub[-_]?agent", "agent", s, flags=re.IGNORECASE)
    norm = re.sub(r"[-_\s]+", "_", s).lower().strip()

    if (
        norm in (
            "search_library_pipeline_node",
            "search_online_pipeline_node",
            "system",
        )
        or norm.endswith(("_node", "_router", "_checkpoint"))
        or norm.startswith(("node_", "checkpoint_"))
        or "pipeline" in norm
        or "checkpoint" in norm
    ):
        return None

    if norm in ("coordinator_agent", "coordinator"):
        return "coordinator"
    if norm in ("library_search_agent", "library_search", "search_agent", "search"):
        return "library_search"
    if norm in (
        "library_synthesizer_agent",
        "library_synthesizer",
        "synthesizer_agent",
        "synthesizer",
    ):
        return "library_synthesizer"
    if norm in ("online_search_agent", "online_search", "discovery_search_agent", "discovery_search"):
        return "online_search"
    if norm in (
        "online_synthesizer_agent",
        "online_synthesizer",
        "discovery_synthesizer_agent",
        "discovery_synthesizer",
    ):
        return "online_synthesizer"
    return norm or "coordinator"


def _normalize_model_name(model_name: str | None) -> str:
    """Normalize model identifier for catalog lookup."""
    if not model_name:
        return ""
    norm = str(model_name).strip()
    for prefix in ("openrouter/", "openrouter_llm/", "litellm/"):
        if norm.startswith(prefix):
            norm = norm[len(prefix):]
    return norm


def _get_or_build_catalog_index(zotero_data_dir: Path | str | None = None) -> dict[str, tuple[float, float, float]]:
    """Build and return an O(1) rate lookup dictionary from the OpenRouter models catalog."""
    global _CATALOG_INDEX
    if _CATALOG_INDEX:
        return _CATALOG_INDEX

    raw_models: list[dict] = []

    # 1. Check in-memory cache in app.llm
    try:
        from zocrates.llm import _OPENROUTER_LLM_CACHE
        if _OPENROUTER_LLM_CACHE.get("data"):
            raw_models = _OPENROUTER_LLM_CACHE["data"]
    except Exception:
        pass

    # 2. Check disk cache
    if not raw_models:
        try:
            from zocrates.config import get_zocrates_data_dir
            candidates: list[Path] = []
            if zotero_data_dir:
                p = Path(zotero_data_dir)
                candidates.append(p / "openrouter_models_cache" / "llm_models.json")
                candidates.append(p / "zocrates-data" / "openrouter_models_cache" / "llm_models.json")
            try:
                candidates.append(get_zocrates_data_dir(zotero_data_dir) / "openrouter_models_cache" / "llm_models.json")
            except Exception:
                pass

            for cache_file in candidates:
                if cache_file.exists():
                    with open(cache_file, "r", encoding="utf-8") as f:
                        cached = json.load(f)
                        raw_models = cached.get("data", [])
                        if raw_models:
                            break
        except Exception as exc:
            logger.debug("Could not load OpenRouter disk cache for pricing: %s", exc)

    index: dict[str, tuple[float, float, float]] = {}
    slug_map: dict[str, list[tuple[float, float, float]]] = {}

    for m in raw_models:
        mid = m.get("id")
        if not mid or not isinstance(mid, str):
            continue

        pricing = m.get("pricing") or {}
        try:
            prompt_rate = float(pricing.get("prompt", 0.0) or 0.0)
        except (ValueError, TypeError):
            prompt_rate = 0.0

        try:
            comp_rate = float(pricing.get("completion", 0.0) or 0.0)
        except (ValueError, TypeError):
            comp_rate = 0.0

        try:
            cache_rate_raw = pricing.get("input_cache_read")
            cache_rate = float(cache_rate_raw) if cache_rate_raw is not None else prompt_rate
        except (ValueError, TypeError):
            cache_rate = prompt_rate

        rates = (prompt_rate, comp_rate, cache_rate)

        # Index by exact and normalized keys
        index[mid] = rates
        index[mid.lower()] = rates
        clean_mid = mid.lstrip("~")
        index[clean_mid] = rates
        index[clean_mid.lower()] = rates

        # Track slugs for unambiguous matches
        if "/" in mid:
            slug = mid.split("/")[-1].lstrip("~").lower()
            slug_map.setdefault(slug, []).append(rates)

    for slug, rate_list in slug_map.items():
        if len(rate_list) == 1 and slug not in index:
            index[slug] = rate_list[0]

    _CATALOG_INDEX = index
    return _CATALOG_INDEX


def get_model_rates(
    model_name: str | None,
    zotero_data_dir: Path | str | None = None,
) -> tuple[float, float, float]:
    """Resolve token rates for a model.

    Returns:
        (prompt_rate_per_token, completion_rate_per_token, cache_read_rate_per_token)
        in USD.
    """
    if not model_name:
        return (0.0, 0.0, 0.0)

    raw_norm = str(model_name).strip()
    lowered = raw_norm.lower()

    # 1. Local & Free models
    if lowered.endswith(":free"):
        return (0.0, 0.0, 0.0)
    if any(lowered.startswith(p) for p in ("ollama/", "ollama_chat/", "vllm/", "local/", "local:", "test/")):
        return (0.0, 0.0, 0.0)
    if lowered in ("local", "ollama", "vllm", "free", "none"):
        return (0.0, 0.0, 0.0)

    # Local provider dispatch check (vLLM, Ollama, OpenAI-compatible local servers)
    try:
        from zocrates.config import load_zocrates_config
        cfg = load_zocrates_config(zotero_data_dir)
        provider = str(cfg.get("llm_provider") or "").lower()
        if provider in ("ollama", "vllm", "openai_compatible"):
            if lowered.startswith(("openai/", "ollama/", "ollama_chat/", "vllm/", "local/")) or not lowered.startswith("openrouter/"):
                return (0.0, 0.0, 0.0)
    except Exception:
        pass

    norm_id = _normalize_model_name(raw_norm)
    cache_key = norm_id.lstrip("~").lower()

    if cache_key in _RATE_CACHE:
        return _RATE_CACHE[cache_key]

    # 2. OpenRouter Catalog (Primary Ground Truth)
    catalog = _get_or_build_catalog_index(zotero_data_dir)
    for key_cand in (norm_id, norm_id.lower(), norm_id.lstrip("~"), cache_key):
        if key_cand in catalog:
            rate = catalog[key_cand]
            _RATE_CACHE[cache_key] = rate
            return rate

    # Try slug if model_name contains slash
    if "/" in norm_id:
        slug = norm_id.split("/")[-1].lstrip("~").lower()
        if slug in catalog:
            rate = catalog[slug]
            _RATE_CACHE[cache_key] = rate
            return rate

    # 3. LiteLLM secondary fallback
    try:
        import litellm
        prompt_cost, comp_cost = litellm.cost_per_token(
            model=norm_id, prompt_tokens=1000, completion_tokens=1000
        )
        p_rate = float(prompt_cost) / 1000.0
        c_rate = float(comp_cost) / 1000.0
        rate = (p_rate, c_rate, p_rate)
        _RATE_CACHE[cache_key] = rate
        return rate
    except Exception:
        pass

    # 4. Safe fallback
    rate = (0.0, 0.0, 0.0)
    _RATE_CACHE[cache_key] = rate
    return rate


def calculate_event_cost(
    model_name: str | None,
    prompt_tokens: int,
    candidates_tokens: int,
    cached_tokens: int = 0,
    zotero_data_dir: Path | str | None = None,
) -> float:
    """Calculate the USD cost for a single model generation event.

    Args:
        model_name: The identifier of the model used.
        prompt_tokens: Total prompt token count (including cached tokens).
        candidates_tokens: Completion token count (includes thoughts; not double-counted).
        cached_tokens: Number of prompt tokens read from cache.
        zotero_data_dir: Optional Zotero data directory for catalog discovery.

    Returns:
        USD cost as a float.
    """
    if not model_name:
        return 0.0
    lowered = str(model_name).lower().strip()
    if lowered.endswith(":free") or any(lowered.startswith(p) for p in ("ollama/", "ollama_chat/", "vllm/", "local/", "local:", "test/")):
        return 0.0

    prompt_rate, completion_rate, cache_read_rate = get_model_rates(
        model_name, zotero_data_dir=zotero_data_dir
    )
    prompt_tokens = max(0, int(prompt_tokens or 0))
    cached_tokens = max(0, int(cached_tokens or 0))
    candidates_tokens = max(0, int(candidates_tokens or 0))

    uncached_prompt = max(0, prompt_tokens - cached_tokens)
    cost = (
        (uncached_prompt * prompt_rate)
        + (cached_tokens * cache_read_rate)
        + (candidates_tokens * completion_rate)
    )
    return float(cost)


def aggregate_turn_cost(
    assistant_events: list[Any],
    default_model: str | None = None,
    zotero_data_dir: Path | str | None = None,
) -> tuple[float, dict[str, str]]:
    """Calculate aggregated USD cost and resolve agent model mapping for an assistant turn.

    Args:
        assistant_events: Assistant event objects in the conversational turn.
        default_model: Fallback model identifier.
        zotero_data_dir: Optional Zotero data directory.

    Returns:
        Tuple of (turn_cost_usd, agent_models_dict).
    """
    from zocrates.utils.session_formatting import parse_usage_metadata

    role_overrides: dict[str, Any] = {}
    fallback_model = default_model

    try:
        from zocrates.config import DEFAULT_LLM_MODEL, load_zocrates_config
        cfg = load_zocrates_config(zotero_data_dir)
        if not fallback_model:
            fallback_model = cfg.get("llm_model") or DEFAULT_LLM_MODEL
        role_overrides = cfg.get("override_agent_models") or {}
    except Exception:
        if not fallback_model:
            fallback_model = "deepseek/deepseek-chat"

    agent_models: dict[str, str] = {}
    total_turn_cost = 0.0

    for evt in assistant_events:
        raw_author = getattr(evt, "author", None)
        role = normalize_agent_role(raw_author)
        if not role or role == "user":
            continue

        evt_model = getattr(evt, "model_version", None)
        if not evt_model:
            # Check configured overrides by role
            if role == "coordinator":
                evt_model = role_overrides.get("coordinator", {}).get("model") or fallback_model
            elif role == "library_search":
                evt_model = role_overrides.get("library_search", {}).get("model") or fallback_model
            elif role == "library_synthesizer":
                evt_model = role_overrides.get("library_synthesizer", {}).get("model") or fallback_model
            elif role == "online_search":
                evt_model = role_overrides.get("online_search", {}).get("model") or fallback_model
            elif role == "online_synthesizer":
                evt_model = role_overrides.get("online_synthesizer", {}).get("model") or fallback_model
            else:
                evt_model = fallback_model

        agent_models[role] = evt_model

        u = parse_usage_metadata(getattr(evt, "usage_metadata", None))
        if u:
            p_cnt = u.get("prompt_tokens", 0)
            c_cnt = u.get("candidates_tokens", 0)
            ca_cnt = u.get("cached_tokens", 0)
            evt_cost = calculate_event_cost(
                evt_model,
                prompt_tokens=p_cnt,
                candidates_tokens=c_cnt,
                cached_tokens=ca_cnt,
                zotero_data_dir=zotero_data_dir,
            )
            total_turn_cost += evt_cost

    if not agent_models and fallback_model:
        agent_models["coordinator"] = fallback_model

    return total_turn_cost, agent_models
