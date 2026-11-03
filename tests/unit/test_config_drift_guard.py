"""Phase 7 Stage A config tests: Recommended-tier drift guard, SSOT materialization,
and preset slug-resolution audit.

Sources of truth:
  - artifacts/plans/model_pricing_tiers_plan.md (Recommended tier JSON block)
  - artifacts/plans/onboarding_configuration_step_plan.md (§5.1/§5.2/§8/§12.4/§12.5)
"""

import copy
import json
import re
from pathlib import Path
from unittest.mock import patch

from zocrates.config import (
    DEFAULT_CONFIG,
    DEFAULT_OVERRIDE_AGENT_MODELS,
    DEFAULT_SYNC_AUTO_ABORT_FAILURES,
    load_zocrates_config,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
TIER_PLAN_PATH = (
    REPO_ROOT / "artifacts" / "plans" / "model_pricing_tiers_plan.md"
)

# Keys of the Recommended tier JSON that deliberately do NOT match DEFAULT_CONFIG.
# Each entry is a documented deviation; anything not listed here must match the
# tier JSON exactly (drift guard).
DOCUMENTED_DEVIATIONS = {
    # Plan §5.2 deviation #1: local embeddings are skip-safe (no API key needed);
    # the Recommended preset card still sets cloud voyage-4 in the wizard.
    "embedding_provider": ("deviation", "local"),
    "embedding_model": ("deviation", "jinaai/jina-embeddings-v5-text-nano"),
    # Local LLMs hardening (Pass 1): subagents default to model: None for uniform
    # model inheritance across local and remote providers.
    "override_agent_models": ("deviation", DEFAULT_OVERRIDE_AGENT_MODELS),
}


def _load_recommended_tier_json() -> dict:
    """Parse the Recommended tier JSON block from model_pricing_tiers_plan.md.

    The first ```json fenced block in the plan file is the Recommended tier
    (the Free tier block follows it). Raises if the block disappears or stops
    being machine-parseable so the drift guard fails loudly.
    """
    text = TIER_PLAN_PATH.read_text(encoding="utf-8")
    blocks = re.findall(r"```json\n(.*?)```", text, re.DOTALL)
    assert blocks, (
        f"No ```json blocks found in {TIER_PLAN_PATH}; the Recommended tier "
        "definition must remain a machine-parseable JSON block."
    )
    return json.loads(blocks[0])


# ---------------------------------------------------------------------------
# Drift guard: DEFAULT_CONFIG == Recommended tier JSON (except documented deviations)
# ---------------------------------------------------------------------------
def test_default_config_matches_recommended_tier_except_documented_deviations():
    tier = _load_recommended_tier_json()
    for key, tier_value in tier.items():
        if key in DOCUMENTED_DEVIATIONS:
            continue
        assert DEFAULT_CONFIG[key] == tier_value, (
            f"DEFAULT_CONFIG['{key}'] drifted from the Recommended tier JSON in "
            f"{TIER_PLAN_PATH.name}: {DEFAULT_CONFIG[key]!r} != {tier_value!r}. "
            "Update zocrates/config.py consciously (plan §5.2 alignment table)."
        )


def test_documented_deviations_hold_their_documented_values():
    """Pin the documented deviations so they cannot drift silently either."""
    tier = _load_recommended_tier_json()
    assert tier["embedding_provider"] == "cloud"  # preset differs from skip-safe defaults
    assert DEFAULT_CONFIG["embedding_provider"] == "local"
    assert DEFAULT_CONFIG["embedding_model"] == "jinaai/jina-embeddings-v5-text-nano"
    assert DEFAULT_CONFIG["override_agent_models"] == DEFAULT_OVERRIDE_AGENT_MODELS
    for role_cfg in DEFAULT_CONFIG["override_agent_models"].values():
        assert role_cfg["model"] is None


def test_converter_default_stays_markitdown():
    """Plan §5.2 deviation #2: skip must not opt into the heavier Docling converter."""
    assert DEFAULT_CONFIG["document_converter_id"] == "markitdown"
    assert DEFAULT_CONFIG["document_converter_mode"] == "local"


# ---------------------------------------------------------------------------
# SSOT materialization: embedding_dimension + sync_auto_abort_failures
# ---------------------------------------------------------------------------
def test_ssot_keys_materialized_in_default_config():
    assert DEFAULT_CONFIG["embedding_dimension"] == 768
    assert DEFAULT_CONFIG["sync_auto_abort_failures"] == DEFAULT_SYNC_AUTO_ABORT_FAILURES
    assert DEFAULT_SYNC_AUTO_ABORT_FAILURES == 10  # plan §8 default


def test_ssot_keys_backfilled_to_existing_config_files(tmp_path):
    config_path = tmp_path / "zocrates-data" / "zocrates_config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with open(config_path, "w") as f:
        json.dump({"llm_model": "openrouter/~z-ai/glm-flash-latest"}, f)

    loaded = load_zocrates_config(tmp_path)
    assert loaded["embedding_dimension"] == 768
    assert loaded["sync_auto_abort_failures"] == 10
    assert (
        loaded["vlm_captioning_backup_model"]
        == DEFAULT_CONFIG["vlm_captioning_backup_model"]
    )
    assert loaded["vlm_captioning_model"] == DEFAULT_CONFIG["vlm_captioning_model"]
    assert (
        loaded["vlm_captioning_provider"]
        == DEFAULT_CONFIG["vlm_captioning_provider"]
    )
    assert loaded["enable_vlm_captioning"] == DEFAULT_CONFIG["enable_vlm_captioning"]
    assert (
        loaded["vlm_max_images_per_doc"]
        == DEFAULT_CONFIG["vlm_max_images_per_doc"]
    )

    # Physically backfilled on disk under FileLock
    with open(config_path) as f:
        disk = json.load(f)
    assert disk["embedding_dimension"] == 768
    assert disk["sync_auto_abort_failures"] == 10
    assert (
        disk["vlm_captioning_backup_model"]
        == DEFAULT_CONFIG["vlm_captioning_backup_model"]
    )
    assert (
        disk["vlm_max_images_per_doc"]
        == DEFAULT_CONFIG["vlm_max_images_per_doc"]
    )

    # User values on disk are preserved (no clobbering)
    with open(config_path, "w") as f:
        json.dump(
            {
                "embedding_dimension": 1024,
                "sync_auto_abort_failures": 3,
                "vlm_captioning_backup_model": "openrouter/custom/backup-model",
                "vlm_max_images_per_doc": 42,
            },
            f,
        )
    reloaded = load_zocrates_config(tmp_path)
    assert reloaded["embedding_dimension"] == 1024
    assert reloaded["sync_auto_abort_failures"] == 3
    assert reloaded["vlm_captioning_backup_model"] == "openrouter/custom/backup-model"
    assert reloaded["vlm_max_images_per_doc"] == 42


def test_vlm_captioning_backup_model_backfilled_from_defaults(tmp_path):
    """Verify vlm_captioning_backup_model is backfilled from defaults if missing on disk."""
    config_path = tmp_path / "zocrates-data" / "zocrates_config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with open(config_path, "w") as f:
        json.dump({"llm_model": "openrouter/~z-ai/glm-flash-latest"}, f)

    loaded = load_zocrates_config(tmp_path)
    assert (
        loaded["vlm_captioning_backup_model"]
        == DEFAULT_CONFIG["vlm_captioning_backup_model"]
    )

    # Verify physically written to disk
    with open(config_path) as f:
        disk = json.load(f)
    assert (
        disk["vlm_captioning_backup_model"]
        == DEFAULT_CONFIG["vlm_captioning_backup_model"]
    )

    # User-specified value is preserved without overwrite
    custom_model = "openrouter/nvidia/nemotron-3.5-super-vl:free"
    with open(config_path, "w") as f:
        json.dump({"vlm_captioning_backup_model": custom_model}, f)

    reloaded = load_zocrates_config(tmp_path)
    assert reloaded["vlm_captioning_backup_model"] == custom_model


def test_vlm_max_images_per_doc_backfilled_from_defaults(tmp_path):
    """Verify vlm_max_images_per_doc is backfilled from DEFAULT_CONFIG if missing on disk,
    and preserves an existing user value.
    """
    config_path = tmp_path / "zocrates-data" / "zocrates_config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with open(config_path, "w") as f:
        json.dump({"llm_model": "openrouter/~z-ai/glm-flash-latest"}, f)

    loaded = load_zocrates_config(tmp_path)
    assert (
        loaded["vlm_max_images_per_doc"]
        == DEFAULT_CONFIG["vlm_max_images_per_doc"]
    )
    assert loaded["vlm_max_images_per_doc"] == 100

    # Verify physically written to disk
    with open(config_path) as f:
        disk = json.load(f)
    assert (
        disk["vlm_max_images_per_doc"]
        == DEFAULT_CONFIG["vlm_max_images_per_doc"]
    )
    assert disk["vlm_max_images_per_doc"] == 100

    # User-specified value is preserved without overwrite
    custom_val = 42
    with open(config_path, "w") as f:
        json.dump({"vlm_max_images_per_doc": custom_val}, f)

    reloaded = load_zocrates_config(tmp_path)
    assert reloaded["vlm_max_images_per_doc"] == custom_val


# ---------------------------------------------------------------------------
# §12.4 write-path verification: override_agent_models is accepted by the
# existing config-update endpoint contract (ConfigRequest → POST /config
# → save_zocrates_config). This asserts the schema + merge path at the unit
# level; the endpoint-level test lives in test_api_endpoints.py
# (TestLLMEndpoints.test_post_llm_config already exercises the full request).
# ---------------------------------------------------------------------------
def test_override_agent_models_write_path_via_save_zocrates_config(tmp_path):
    from zocrates.config import save_zocrates_config

    tier = _load_recommended_tier_json()
    overrides = copy.deepcopy(tier["override_agent_models"])

    save_zocrates_config({"override_agent_models": overrides}, tmp_path)

    disk = json.loads((tmp_path / "zocrates-data" / "zocrates_config.json").read_text())
    assert disk["override_agent_models"]["library_synthesizer"] == {
        "model": "openrouter/google/gemini-3.8-flash",
        "thinking_level": "high",
    }
    assert disk["override_agent_models"]["coordinator"]["model"] is None
    # Unspecified roles are merged with defaults rather than dropped
    assert "online_search" in disk["override_agent_models"]


# ---------------------------------------------------------------------------
# §12.5 slug-resolution audit table: every preset config key's resolution path
# ---------------------------------------------------------------------------
def test_preset_slug_resolution_table(tmp_path):
    """Assert the resolution path for every Recommended-tier model key.

    Table (plan §12.5 audit):
      | Key                          | Resolution path                              |
      |------------------------------|----------------------------------------------|
      | llm_model                    | "openrouter/..." passes _resolve_model_name unchanged; bare slugs gain the openrouter/ prefix; "~vendor/model" aliases pass through LiteLLM unmodified |
      | override_agent_models        | same _resolve_model_name passthrough for pinned ids |
      | reranker_model               | consumed verbatim as the rerank-endpoint model id (no prefixing; "~" aliases structurally impossible here) |
      | citation_verification_model  | unprefixed slug used verbatim in the Decisions API payload |
      | predictive_prompt_model      | "openrouter/~deepseek/..." passes through to litellm.acompletion unmodified |
      | compaction_summarizer_model  | same as predictive_prompt_model              |
      | vlm_captioning_model         | used verbatim in the OpenRouter chat payload |
      | embedding_model (cloud)      | OpenRouterEmbedder normalizes unprefixed → "openrouter/..." |
    """
    from zocrates.config import (
        DEFAULT_CITATION_VERIFICATION_MODEL,
        DEFAULT_COMPACTION_SUMMARIZER_MODEL,
        DEFAULT_PREDICTIVE_PROMPT_MODEL,
        DEFAULT_RERANKER_MODEL,
        DEFAULT_VLM_CAPTIONING_MODEL,
    )
    from zocrates.llm import _resolve_model_name

    # --- llm_model / override_agent_models: prefixed ids pass through unchanged
    assert (
        _resolve_model_name("openrouter/~z-ai/glm-flash-latest", "openrouter_llm")
        == "openrouter/~z-ai/glm-flash-latest"
    )
    # Bare slugs resolve with the openrouter/ prefix (current behavior)
    assert (
        _resolve_model_name("z-ai/glm-flash-latest", "openrouter_llm")
        == "openrouter/z-ai/glm-flash-latest"
    )
    # Bare "~" alias gains only the provider prefix; the "~" reaches LiteLLM intact
    assert (
        _resolve_model_name("~z-ai/glm-flash-latest", "openrouter_llm")
        == "openrouter/~z-ai/glm-flash-latest"
    )
    # Synthesizer default override is None (inherits global model)
    assert DEFAULT_OVERRIDE_AGENT_MODELS["library_synthesizer"]["model"] is None
    # If explicitly pinned, passes through unchanged
    assert (
        _resolve_model_name("openrouter/google/gemini-3.8-flash", "openrouter_llm")
        == "openrouter/google/gemini-3.8-flash"
    )
    # Remote-only auxiliary defaults are "openrouter/"-prefixed → LiteLLM passthrough
    assert _resolve_model_name(
        DEFAULT_PREDICTIVE_PROMPT_MODEL, "openrouter_llm"
    ) == DEFAULT_PREDICTIVE_PROMPT_MODEL
    assert _resolve_model_name(
        DEFAULT_COMPACTION_SUMMARIZER_MODEL, "openrouter_llm"
    ) == DEFAULT_COMPACTION_SUMMARIZER_MODEL

    # --- reranker_model: verbatim rerank-endpoint id; "~" aliases impossible here
    assert DEFAULT_RERANKER_MODEL == "qwen/qwen3-reranker-8b"
    assert "~" not in DEFAULT_RERANKER_MODEL
    assert not DEFAULT_RERANKER_MODEL.startswith("openrouter/")

    # --- citation_verification_model: unprefixed slug used verbatim
    assert DEFAULT_CITATION_VERIFICATION_MODEL == "inception/mercury-decide"

    # --- vlm_captioning_model: "openrouter/..." used verbatim
    assert DEFAULT_VLM_CAPTIONING_MODEL == "openrouter/deepseek/deepseek-v4.1-flash"


def test_cloud_embedding_slug_normalization(tmp_path):
    """Cloud preset embedding model normalizes to an openrouter/-prefixed id."""
    from zocrates.rag.embeddings import OpenRouterEmbedder

    with patch("zocrates.config.load_zocrates_config", return_value={}):
        embedder = OpenRouterEmbedder(model_name="voyageai/voyage-4", dimension=1024)
    assert embedder._model_name == "openrouter/voyageai/voyage-4"
    # Already-prefixed ids pass through unmodified
    with patch("zocrates.config.load_zocrates_config", return_value={}):
        embedder_prefixed = OpenRouterEmbedder(
            model_name="openrouter/voyageai/voyage-4", dimension=1024
        )
    assert embedder_prefixed._model_name == "openrouter/voyageai/voyage-4"


def test_openrouter_provider_default_consistent_with_main_default():
    from zocrates.config import DEFAULT_LLM_MODEL, PROVIDER_DEFAULT_MODELS

    assert PROVIDER_DEFAULT_MODELS["openrouter"] == DEFAULT_LLM_MODEL
    assert PROVIDER_DEFAULT_MODELS["openrouter_llm"] == DEFAULT_LLM_MODEL

# ---------------------------------------------------------------------------
# Release versioning: pyproject.toml <-> manifest.json drift guard + __version__
# ---------------------------------------------------------------------------
def test_pyproject_version_matches_manifest_json():
    """The extension manifest version must stay in sync with pyproject.toml.

    bootstrap.js derives the versioned wheel install target
    (zocrates-<version>-py3-none-any.whl) from manifest.json, while
    local_deploy.sh builds the wheel from pyproject.toml — a drift breaks
    wheel installation and the GitHub Actions release tag guard.
    """
    import tomllib

    pyproject_version = tomllib.loads(
        (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]["version"]
    manifest = json.loads(
        (REPO_ROOT / "zotero-plugin" / "addon" / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["version"] == pyproject_version


def test_zocrates_exposes_version():
    """`zocrates.__version__` resolves via importlib.metadata (wheel install).

    The backend /status endpoint reports this value so the Zotero frontend can
    verify the running backend matches the bundled extension version. When the
    package is not installed (dev checkout without `uv sync`), it falls back
    to the "0.0.0+unknown" sentinel instead of raising.
    """
    from zocrates import __version__

    assert isinstance(__version__, str)
    assert __version__
    assert not __version__.startswith("0.0.0+unknown"), (
        "zocrates is expected to be installed in the test venv; the "
        "PackageNotFoundError fallback fired instead of resolving metadata."
    )
