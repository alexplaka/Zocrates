"""
Embedding functions for the vector store.

Supports two modes:
  - "local"   → Jina v5 (jina-embeddings-v5-text-nano), runs entirely offline.
  - "cloud"   → OpenRouter-hosted embedding models (e.g. voyage-4,
                nemotron-3-embed-1b:free), requires an OpenRouter API key.
"""

import os
import re
import logging
import threading
import time
from typing import Any, Protocol

from zocrates.config import (
    EMBEDDING_DIMENSION,
    EMBEDDING_MODE,
    LOCAL_EMBEDDING_MODEL,
)
from zocrates.rag.hardware import _detect_best_device

logger = logging.getLogger(__name__)

MAX_EMBEDDING_DIMENSION: int = 4096
MIN_EMBEDDING_DIMENSION: int = 64


class EmbedFn(Protocol):
    """Callable protocol accepted by VectorStore."""

    def __call__(
        self,
        texts: list[str],
        task: str = "retrieval",
        prompt_name: str | None = None,
    ) -> list[list[float]]:
        """Compute embedding vectors for a list of input texts.

        Args:
            texts: List of text strings to embed.
            task: Task descriptor (e.g. 'retrieval', 'classification').
            prompt_name: Optional prompt variant name (e.g. 'document', 'query').

        Returns:
            List of float embedding vectors.
        """
        ...


def _safe_to(module: Any, device: Any, dtype: Any) -> Any:
    """Recursively moves module parameters/buffers to device/dtype, skipping meta tensors.

    Args:
        module: PyTorch module to move.
        device: Target torch device or device string.
        dtype: Target torch data type.

    Returns:
        The moved module.

    Raises:
        Exception: If moving to a non-CPU device fails due to hardware or driver errors.
    """
    try:
        return module.to(device=device, dtype=dtype)
    except Exception as exc:
        # If moving to a non-CPU device failed due to hardware/runtime issues (e.g. OOM, driver),
        # raise so that opportunistic GPU callers can gracefully fall back to CPU.
        dev_type = getattr(device, "type", str(device))
        if dev_type != "cpu" and "meta" not in str(exc).lower():
            raise
        for child in module.children():
            _safe_to(child, device, dtype)
        for name, param in getattr(module, "_parameters", {}).items():
            if param is not None and getattr(param, "device", None) and getattr(param.device, "type", "") != "meta":
                module._parameters[name] = param.to(device=device, dtype=dtype)
        for name, buf in getattr(module, "_buffers", {}).items():
            if buf is not None and getattr(buf, "device", None) and getattr(buf.device, "type", "") != "meta":
                module._buffers[name] = buf.to(device=device, dtype=dtype)
        return module


# ---------------------------------------------------------------------------
# Local embedder (transformers AutoModel with Jina v5)
# ---------------------------------------------------------------------------

_SHARED_LOCAL_MODEL: Any = None
_SHARED_LOCAL_DIMENSION: int = 768
_SHARED_LOCAL_DEVICE: Any = None
_SHARED_LOCAL_LOCK: threading.Lock = threading.Lock()


class LocalEmbedder:
    """Wraps Hugging Face jina-embeddings-v5-text-nano model with opportunistic GPU acceleration and graceful CPU fallback."""

    def __init__(
        self,
        model_name: str = LOCAL_EMBEDDING_MODEL,
        dimension: int = EMBEDDING_DIMENSION,
    ) -> None:
        """Initialize the local embedding model wrapper.

        Args:
            model_name: Hugging Face model identifier string.
            dimension: Target embedding output dimension.
        """
        self.requested_model_name = "jinaai/jina-embeddings-v5-text-nano"
        self._model_name = "jinaai/jina-embeddings-v5-text-nano"
        self._dimension = 768
        self._model = None
        self._device: Any = None
        self.is_fallback = False
        self.validation_error: str | None = None
        self._load_lock = threading.Lock()

    def _load(self) -> Any:
        """Return the loaded model, initializing it on first call (thread-safe).

        Uses double-checked locking to ensure the model is instantiated exactly
        once across all LocalEmbedder instances.

        Returns:
            The loaded Hugging Face AutoModel instance.
        """
        global _SHARED_LOCAL_MODEL, _SHARED_LOCAL_DIMENSION, _SHARED_LOCAL_DEVICE
        self._dimension = 768

        if self._model is not None:
            return self._model
        if _SHARED_LOCAL_MODEL is not None:
            self._model = _SHARED_LOCAL_MODEL
            self._dimension = 768
            self._device = _SHARED_LOCAL_DEVICE
            return self._model

        with _SHARED_LOCAL_LOCK:
            if _SHARED_LOCAL_MODEL is not None:
                self._model = _SHARED_LOCAL_MODEL
                self._dimension = 768
                self._device = _SHARED_LOCAL_DEVICE
                return self._model

            # Self-heal corrupted config dimension once on initial model load if local provider is active
            try:
                from zocrates.config import load_zocrates_config, save_zocrates_config

                cfg = load_zocrates_config()
                if cfg.get("embedding_provider", "local") == "local" and cfg.get("embedding_dimension") != 768:
                    save_zocrates_config({"embedding_dimension": 768})
            except Exception:
                pass

            model = self._load_model_internal()
            _SHARED_LOCAL_MODEL = model
            self._dimension = 768
            _SHARED_LOCAL_DIMENSION = 768
            _SHARED_LOCAL_DEVICE = self._device
            self._model = model
            return self._model

    def _load_model_internal(self) -> Any:
        """Download (or load from cache) the Jina v5 model and move it to the best available device.

        Opportunistically targets GPU (CUDA/MPS) with graceful fallback to CPU.
        Limits PyTorch CPU thread count to 4 to balance throughput against
        OpenMP lock contention during concurrent embedding calls.

        Returns:
            The initialized AutoModel set to ``float32`` on the selected device in eval mode.
        """
        import torch
        from transformers import AutoModel

        # Limit PyTorch CPU threads to 4 to maximize throughput while avoiding OpenMP lock contention
        if hasattr(torch, "set_num_threads"):
            torch.set_num_threads(min(4, os.cpu_count() or 4))

        target_device = _detect_best_device()
        target_dtype = torch.float32

        logger.info("Loading local embedding model: %s on %s", self._model_name, target_device)

        model = AutoModel.from_pretrained(
            self._model_name,
            trust_remote_code=True,
            dtype=target_dtype,
        )

        try:
            model = _safe_to(model, target_device, target_dtype)
            self._device = target_device
        except Exception as exc:
            logger.warning(
                "Failed to move embedding model to %s (%s: %s). Falling back to CPU.",
                target_device,
                type(exc).__name__,
                exc,
            )
            cpu_device = torch.device("cpu")
            model = _safe_to(model, cpu_device, target_dtype)
            self._device = cpu_device

        if hasattr(model, "eval"):
            model.eval()

        if hasattr(model, "config") and hasattr(model.config, "hidden_size"):
            self._dimension = getattr(model.config, "hidden_size", 768) or 768
        else:
            self._dimension = 768

        global _SHARED_LOCAL_DEVICE
        _SHARED_LOCAL_DEVICE = self._device

        return model

    def __call__(
        self,
        texts: list[str],
        task: str = "retrieval",
        prompt_name: str | None = None,
    ) -> list[list[float]]:
        """Encode a batch of texts into embedding vectors.

        Args:
            texts: List of input strings to embed.
            task: Embedding task hint passed to the model (e.g. ``"retrieval"``).
            prompt_name: Optional prompt variant (e.g. ``"query"`` or
                ``"document"``). Defaults to ``"document"`` if ``None``.

        Returns:
            List of embedding vectors, one float list per input text.
        """
        model = self._load()
        return self._encode_internal(model, texts, task, prompt_name)

    def _run_batches(
        self, model: Any, texts: list[str], task: str, prompt_name: str | None
    ) -> list[list[float]]:
        """Encode texts in mini-batches to prevent GPU/CPU memory exhaustion.

        Args:
            model: The loaded AutoModel instance.
            texts: List of text strings to encode.
            task: Embedding task hint.
            prompt_name: Optional prompt variant name.

        Returns:
            List of float embedding vectors.
        """
        import gc
        import numpy as np
        import torch

        p_name = prompt_name or "document"
        batch_size = 8
        all_embeddings: list[list[float]] = []

        with torch.inference_mode():
            for i in range(0, len(texts), batch_size):
                batch_texts = texts[i : i + batch_size]
                if hasattr(model, "encode"):
                    try:
                        embs = model.encode(
                            texts=batch_texts,
                            task=task,
                            prompt_name=p_name,
                            truncate_dim=self._dimension,
                        )
                    except (TypeError, AttributeError):
                        try:
                            embs = model.encode(batch_texts)
                        except Exception:
                            embs = model(batch_texts)
                else:
                    embs = model(batch_texts)

                if isinstance(embs, torch.Tensor):
                    embs_list = embs.float().cpu().tolist()
                    all_embeddings.extend(embs_list)
                    del embs, embs_list
                elif isinstance(embs, np.ndarray):
                    all_embeddings.extend(embs.tolist())
                    del embs
                elif hasattr(embs, "tolist"):
                    all_embeddings.extend(embs.tolist())
                    del embs
                elif isinstance(embs, list):
                    for e in embs:
                        if isinstance(e, torch.Tensor):
                            all_embeddings.append(e.float().cpu().tolist())
                        elif isinstance(e, (list, tuple)):
                            all_embeddings.append([float(x) for x in e])
                        elif hasattr(e, "tolist"):
                            all_embeddings.append(e.tolist())
                        else:
                            all_embeddings.append(list(map(float, e)))
                    del embs

        if len(texts) > 16:
            gc.collect()

        return all_embeddings

    def _encode_internal(
        self, model: Any, texts: list[str], task: str, prompt_name: str | None
    ) -> list[list[float]]:
        """Run inference on the loaded model and return float embedding vectors.

        If inference on GPU fails at runtime, gracefully falls back to CPU execution.

        Args:
            model: The loaded AutoModel instance.
            texts: List of input strings to embed.
            task: Embedding task hint passed to the model.
            prompt_name: Prompt variant; defaults to ``"document"`` if ``None``.

        Returns:
            List of embedding vectors as plain Python float lists.
        """
        if not texts:
            return []

        import torch

        current_device = getattr(self, "_device", None)
        if current_device is None:
            current_device = torch.device("cpu")
            self._device = current_device

        is_gpu = getattr(current_device, "type", str(current_device)) != "cpu"

        if is_gpu:
            try:
                return self._run_batches(model, texts, task, prompt_name)
            except Exception as exc:
                logger.warning(
                    "Embedding inference failed on %s (%s: %s). Falling back to CPU.",
                    current_device,
                    type(exc).__name__,
                    exc,
                )
                if hasattr(torch, "cuda") and torch.cuda.is_available():
                    try:
                        torch.cuda.empty_cache()
                    except Exception:
                        pass
                cpu_device = torch.device("cpu")
                model = _safe_to(model, cpu_device, torch.float32)
                if hasattr(model, "eval"):
                    model.eval()
                self._device = cpu_device
                self._model = model
                global _SHARED_LOCAL_MODEL, _SHARED_LOCAL_DEVICE
                _SHARED_LOCAL_MODEL = model
                _SHARED_LOCAL_DEVICE = cpu_device
                return self._run_batches(model, texts, task, prompt_name)

        return self._run_batches(model, texts, task, prompt_name)

    @property
    def dimension(self) -> int:
        """Output dimension of the local embedding model."""
        return self._dimension


# ---------------------------------------------------------------------------
# Cloud embedder (OpenRouter)
# ---------------------------------------------------------------------------

KNOWN_EMBEDDING_DIMENSIONS: dict[str, int] = {
    "baai/bge-m3": 1024,
    "nvidia/nemotron-3-embed-1b:free": 1024,
    "nvidia/nemotron-3-embed-1b": 1024,
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
    "text-embedding-ada-002": 1536,
    "jinaai/jina-embeddings-v5-text-nano": 768,
    "text-embedding-004": 768,
    "qwen/qwen-2.5-coder-32b-instruct": 1024,
}


class OpenRouterEmbedder:
    """Wraps OpenRouter embedding models via litellm.embedding with dynamic dimension auto-detection."""

    def __init__(
        self,
        model_name: str | None = None,
        dimension: int | None = None,
        api_key: str | None = None,
    ) -> None:
        """Initialize the OpenRouter cloud embedding model wrapper.

        Args:
            model_name: Optional OpenRouter embedding model identifier.
            dimension: Optional explicit embedding dimension override.
            api_key: Optional OpenRouter API key override.
        """
        from zocrates.config import load_zocrates_config
        cfg = load_zocrates_config()
        self._model_name = model_name or cfg.get("embedding_model", "openrouter/nvidia/nemotron-3-embed-1b:free")
        if not self._model_name.startswith("openrouter/") and not self._model_name.startswith("openai/"):
            self._model_name = f"openrouter/{self._model_name}"
        self._dimension = dimension
        self._api_key = api_key

        if self._dimension is None:
            # Tier 1: Explicit config in zocrates_config.json
            cfg_dim = cfg.get("embedding_dimension")
            if cfg_dim and isinstance(cfg_dim, int):
                self._dimension = cfg_dim

            # Tier 2: Check if ChromaDB already has an existing collection on disk for this model
            if self._dimension is None:
                client = None
                created_client = False
                try:
                    import re
                    from zocrates.config import get_chroma_dir, resolve_zotero_data_dir

                    try:
                        from zocrates.rag.service_manager import get_syncer
                        syncer = get_syncer()
                        if syncer and getattr(syncer, "store", None) and getattr(syncer.store, "_client", None):
                            client = syncer.store._client
                    except Exception:
                        pass

                    if client is None:
                        zdir = resolve_zotero_data_dir()
                        if zdir:
                            cdir = get_chroma_dir(zdir)
                            if cdir.exists():
                                import chromadb
                                client = chromadb.PersistentClient(path=str(cdir))
                                created_client = True

                    if client is not None:
                        sanitized = re.sub(r"[^a-zA-Z0-9_]", "_", self._model_name.strip())
                        sanitized = re.sub(r"_+", "_", sanitized).strip("_")
                        pattern = re.compile(rf"^zotero_library_{re.escape(sanitized)}_(\d+)d$")

                        # In Chroma v1.x, list_collections() returns Collection objects
                        for col_entry in client.list_collections():
                            col_name = (
                                col_entry
                                if isinstance(col_entry, str)
                                else str(getattr(col_entry, "name", col_entry))
                            )
                            m = pattern.match(col_name)
                            if m:
                                self._dimension = int(m.group(1))
                                break

                        # Fallback to collection metadata matching
                        if self._dimension is None:
                            for col_entry in client.list_collections():
                                col_name = (
                                    col_entry
                                    if isinstance(col_entry, str)
                                    else str(getattr(col_entry, "name", col_entry))
                                )
                                try:
                                    if hasattr(col_entry, "metadata") and col_entry.metadata:
                                        meta = col_entry.metadata
                                    else:
                                        col = client.get_collection(name=col_name)
                                        meta = col.metadata or {}
                                    if (
                                        meta.get("model_name") == self._model_name
                                        and meta.get("dimension")
                                    ):
                                        self._dimension = int(meta["dimension"])
                                        break
                                except Exception:
                                    pass
                except Exception:
                    pass
                finally:
                    if created_client and client is not None and hasattr(client, "close"):
                        try:
                            client.close()
                        except Exception:
                            pass

            # Tier 3: Static known dimensions registry
            if self._dimension is None:
                norm_name = self._model_name.removeprefix("openrouter/").removeprefix("openai/")
                for k, d in KNOWN_EMBEDDING_DIMENSIONS.items():
                    if norm_name == k or norm_name.endswith(k):
                        self._dimension = d
                        break

            # Tier 4: LiteLLM model_cost registry
            if self._dimension is None:
                try:
                    import litellm

                    norm_name = self._model_name.removeprefix("openrouter/").removeprefix("openai/")
                    info = (
                        litellm.model_cost.get(norm_name)
                        or litellm.model_cost.get(self._model_name)
                        or litellm.model_cost.get(f"openai/{norm_name}")
                    )
                    if info and info.get("output_vector_size"):
                        self._dimension = int(info["output_vector_size"])
                except Exception:
                    pass

        if self._dimension is not None and self._dimension > MAX_EMBEDDING_DIMENSION:
            logger.warning(
                "Configured embedding dimension %d exceeds ceiling %d. Clamping to %d.",
                self._dimension,
                MAX_EMBEDDING_DIMENSION,
                MAX_EMBEDDING_DIMENSION,
            )
            self._dimension = MAX_EMBEDDING_DIMENSION

    def __call__(
        self,
        texts: list[str],
        task: str = "retrieval",
        prompt_name: str | None = None,
        **kwargs: Any,
    ) -> list[list[float]]:
        """Embed a batch of texts using OpenRouter Cloud Embedding API via LiteLLM.

        Args:
            texts: List of input strings to embed.
            task: Unused; present for interface compatibility.
            prompt_name: Unused; present for interface compatibility.
            **kwargs: Additional keyword arguments (ignored).

        Returns:
            List of embedding vectors, one float list per input text.
        """
        import litellm
        from zocrates.config import get_secure_api_key

        key = self._api_key or get_secure_api_key("openrouter_api_key", ("OPENROUTER_API_KEY", "OPEN_ROUTER_API_KEY"))
        if not key:
            raise ValueError(
                "OPENROUTER_API_KEY is not configured. Please supply an API key in settings or select Local Embeddings."
            )

        headers = {
            "HTTP-Referer": "https://github.com/alexplaka/Zocrates",
            "X-OpenRouter-Title": "Zocrates",
        }

        # Defense-in-depth: sanitize any data:image/ URIs that would cause OpenRouter
        # embedding endpoints to reject the payload with HTTP 400 (VLM multimodal error)
        data_image_regex = re.compile(r'data:image/[^\s"\'\)\]]*')
        sanitized_texts: list[str] = []
        for t in texts:
            if "data:image/" in t:
                cleaned = data_image_regex.sub("[image]", t)
                if "data:image/" in cleaned:
                    cleaned = cleaned.replace("data:image/", "[image]")
                sanitized_texts.append(cleaned)
            else:
                sanitized_texts.append(t)

        max_retries = 3
        base_backoff_sec = 2.0
        response = None
        for attempt in range(max_retries + 1):
            try:
                response = litellm.embedding(
                    model=self._model_name,
                    input=sanitized_texts,
                    api_key=key,
                    extra_headers=headers,
                )
                break
            except Exception as exc:
                err_msg = str(exc).lower()
                is_transient = (
                    (hasattr(litellm, "RateLimitError") and isinstance(exc, litellm.RateLimitError))
                    or (hasattr(litellm, "ServiceUnavailableError") and isinstance(exc, litellm.ServiceUnavailableError))
                    or "rate limit" in err_msg
                    or "overloaded" in err_msg
                    or "status_code: 429" in err_msg
                    or "code: 429" in err_msg
                    or 'code":429' in err_msg
                    or "429 too many requests" in err_msg
                    or "status_code: 503" in err_msg
                    or "code: 503" in err_msg
                    or "503 service unavailable" in err_msg
                )
                if is_transient and attempt < max_retries:
                    wait_sec = base_backoff_sec * (2**attempt)
                    logger.warning(
                        "OpenRouter embedding rate limit / overload encountered (attempt %d/%d): %s. Retrying in %.1fs...",
                        attempt + 1,
                        max_retries,
                        exc,
                        wait_sec,
                    )
                    time.sleep(wait_sec)
                else:
                    raise

        embeddings = [item["embedding"] for item in response.data]

        # Auto-detect dimension on first call if not explicitly set
        if embeddings:
            actual_dim = len(embeddings[0])
            if actual_dim > MAX_EMBEDDING_DIMENSION:
                logger.warning(
                    "Returned embedding dimension %d exceeds ceiling %d.",
                    actual_dim,
                    MAX_EMBEDDING_DIMENSION,
                )
            if self._dimension != actual_dim:
                self._dimension = actual_dim
                logger.info("Auto-detected embedding dimension for %s: %d", self._model_name, self._dimension)
                # Persist valid dimension to zocrates_config.json so future startups need zero network calls
                if MIN_EMBEDDING_DIMENSION <= actual_dim <= MAX_EMBEDDING_DIMENSION:
                    try:
                        from zocrates.config import save_zocrates_config

                        save_zocrates_config({"embedding_dimension": actual_dim})
                    except Exception:
                        pass

        return embeddings

    @property
    def dimension(self) -> int:
        """Output dimension of the OpenRouter embedding model."""
        return self._dimension or 1024


# Alias for backwards compatibility / naming convention
OpenRouterEmbeddings = OpenRouterEmbedder


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def get_embedder(
    mode: str | None = None,
) -> LocalEmbedder | OpenRouterEmbedder:
    """Return the configured embedding function.

    Reads the active mode from the ``EMBEDDING_MODE`` environment variable,
    falling back to the persisted config file value, then to ``"local"``.

    Args:
        mode: Override for the embedding mode (``"local"`` or ``"cloud"``).
            If ``None``, the mode is determined from the environment and
            the Zocrates config file.

    Returns:
        A ``LocalEmbedder`` for CPU-based Jina embeddings, or an
        ``OpenRouterEmbedder`` for OpenRouter cloud embeddings.
    """
    from zocrates.config import load_zocrates_config

    cfg = load_zocrates_config()
    active_mode = mode or os.getenv(
        "EMBEDDING_MODE",
        cfg.get("embedding_provider", "local"),
    )

    if active_mode == "cloud":
        logger.info("Using cloud embedding mode (OpenRouter).")
        return OpenRouterEmbedder()

    logger.info("Using local CPU embedding mode with model jinaai/jina-embeddings-v5-text-nano (768d).")
    return LocalEmbedder()
