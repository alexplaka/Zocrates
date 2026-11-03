"""Unit tests for embedding models."""

import sys
from unittest.mock import MagicMock, patch

import pytest
import numpy as np
import torch
from zocrates.rag.embeddings import LocalEmbedder, OpenRouterEmbedder, OpenRouterEmbeddings
from zocrates.rag.hardware import _detect_best_device


@patch("zocrates.rag.hardware.validate_model_compatibility")
@patch("transformers.AutoModel.from_pretrained")
@patch("torch.device")
def test_local_embedder_mock(mock_device, mock_from_pretrained, mock_validate):
    """Verify LocalEmbedder loads the Jina model and returns correctly shaped vectors.

    Mocks transformers and torch to avoid downloading real model weights.
    Asserts that ``from_pretrained`` is called once and that the result is a
    list of two 768-dimensional float vectors.
    """
    import zocrates.rag.embeddings as emb_module
    emb_module._SHARED_LOCAL_MODEL = None

    mock_validate.return_value = (True, "Success")
    # Setup mock model
    mock_model = MagicMock()
    mock_model.config.hidden_size = 768
    mock_model.to.return_value = mock_model
    mock_model.float.return_value = mock_model
    # Mock model.encode to return a numpy array
    mock_embeddings = np.random.rand(2, 768)
    mock_model.encode.return_value = mock_embeddings
    mock_from_pretrained.return_value = mock_model

    # Initialize embedder
    embedder = LocalEmbedder(model_name="dummy-jina", dimension=768)
    assert embedder.dimension == 768

    # Call embedder
    texts = ["hello", "world"]
    res = embedder(texts, task="retrieval", prompt_name="query")

    # Assert model loaded and encodes called correctly
    mock_from_pretrained.assert_called_once()
    mock_model.encode.assert_called_once_with(
        texts=texts,
        task="retrieval",
        prompt_name="query",
        truncate_dim=768
    )
    assert isinstance(res, list)
    assert len(res) == 2
    assert len(res[0]) == 768


@patch("transformers.AutoModel.from_pretrained")
def test_local_embedder_shared_cache_and_mini_batching(mock_from_pretrained):
    """Verify multiple LocalEmbedder instances share one model and batch large inputs in chunks of 8."""
    import zocrates.rag.embeddings as emb_module
    emb_module._SHARED_LOCAL_MODEL = None

    mock_model = MagicMock()
    mock_model.config.hidden_size = 768
    mock_model.to.return_value = mock_model
    mock_model.encode.side_effect = lambda texts, **kwargs: np.ones((len(texts), 768))
    mock_from_pretrained.return_value = mock_model

    embedder1 = LocalEmbedder()
    embedder2 = LocalEmbedder()

    # Pass 20 texts (should result in 3 mini-batches: 8, 8, 4)
    texts = [f"text_{i}" for i in range(20)]
    res1 = embedder1(texts)
    res2 = embedder2(texts)

    # AutoModel.from_pretrained should only be called once across both instances
    mock_from_pretrained.assert_called_once()
    assert len(res1) == 20
    assert len(res2) == 20
    # mock_model.encode should have been called 3 times per embedder call = 6 total
    assert mock_model.encode.call_count == 6


def test_openrouter_embedder_mock():
    """Verify OpenRouterEmbedder delegates to LiteLLM embedding API and returns embedding values.

    Mocks litellm.embedding to avoid real network calls. Asserts that
    ``litellm.embedding`` is invoked exactly once and auto-detects dimensions.
    """
    with patch("zocrates.config.save_zocrates_config"), patch("zocrates.config.get_secure_api_key", return_value="dummy_key"), patch("litellm.embedding") as mock_litellm_embedding:
        mock_item = {"embedding": [0.1, 0.2, 0.3]}
        mock_response = MagicMock()
        mock_response.data = [mock_item]
        mock_litellm_embedding.return_value = mock_response

        embedder = OpenRouterEmbedder(model_name="openrouter/nvidia/nemotron-3-embed-1b:free")
        res = embedder(["test"])

        mock_litellm_embedding.assert_called_once()
        assert res == [[0.1, 0.2, 0.3]]
        assert embedder.dimension == 3


def test_openrouter_embeddings_retry_backoff_on_rate_limit():
    """Verify OpenRouterEmbeddings retries with exponential backoff on RateLimitError and succeeds."""
    import litellm

    rate_limit_err = litellm.RateLimitError(
        message="Rate limit exceeded: 429 Too Many Requests",
        model="openrouter/nvidia/nemotron-3-embed-1b:free",
        llm_provider="openrouter",
    )
    mock_item = {"embedding": [0.4, 0.5, 0.6]}
    mock_success_response = MagicMock()
    mock_success_response.data = [mock_item]

    with (
        patch("zocrates.config.save_zocrates_config"),
        patch("zocrates.config.get_secure_api_key", return_value="dummy_key"),
        patch("time.sleep") as mock_sleep,
        patch("litellm.embedding", side_effect=[rate_limit_err, mock_success_response]) as mock_embedding,
    ):
        embedder = OpenRouterEmbeddings(model_name="openrouter/nvidia/nemotron-3-embed-1b:free")
        res = embedder(["sample text"])

        assert mock_embedding.call_count == 2
        mock_sleep.assert_called_once_with(2.0)
        assert res == [[0.4, 0.5, 0.6]]
        assert embedder.dimension == 3


def test_openrouter_embeddings_retry_exhausted_raises():
    """Verify OpenRouterEmbeddings re-raises when max_retries are exhausted."""
    import litellm

    rate_limit_err = litellm.RateLimitError(
        message="Rate limit exceeded: 429 Too Many Requests",
        model="openrouter/nvidia/nemotron-3-embed-1b:free",
        llm_provider="openrouter",
    )

    with (
        patch("zocrates.config.save_zocrates_config"),
        patch("zocrates.config.get_secure_api_key", return_value="dummy_key"),
        patch("time.sleep") as mock_sleep,
        patch("litellm.embedding", side_effect=rate_limit_err) as mock_embedding,
    ):
        embedder = OpenRouterEmbeddings(model_name="openrouter/nvidia/nemotron-3-embed-1b:free")
        with pytest.raises(litellm.RateLimitError):
            embedder(["sample text"])

        assert mock_embedding.call_count == 4  # 1 initial + 3 retries
        assert mock_sleep.call_count == 3
        assert [call.args[0] for call in mock_sleep.call_args_list] == [2.0, 4.0, 8.0]


def test_detect_best_device_default_cpu_fallback():
    """Verify that _detect_best_device returns CPU when neither CUDA nor MPS is available."""
    with (
        patch("torch.cuda.is_available", return_value=False),
        patch.object(torch.backends.mps, "is_available", return_value=False, create=True),
    ):
        dev = _detect_best_device()
        assert dev.type == "cpu"


def test_detect_best_device_cuda_success():
    """Verify that _detect_best_device returns CUDA when probe succeeds."""
    with (
        patch("torch.cuda.is_available", return_value=True),
        patch("torch.zeros", return_value=MagicMock()),
    ):
        dev = _detect_best_device()
        assert dev.type == "cuda"


def test_detect_best_device_cuda_failure_fallback():
    """Verify that _detect_best_device falls back to CPU when CUDA probe fails."""
    with (
        patch("torch.cuda.is_available", return_value=True),
        patch("torch.zeros", side_effect=RuntimeError("CUDA probe error")),
        patch.object(torch.backends.mps, "is_available", return_value=False, create=True),
    ):
        dev = _detect_best_device()
        assert dev.type == "cpu"


def test_detect_best_device_mps_success():
    """Verify that _detect_best_device returns MPS when CUDA is unavailable and MPS probe succeeds."""
    with (
        patch("torch.cuda.is_available", return_value=False),
        patch.object(torch.backends.mps, "is_available", return_value=True, create=True),
        patch("torch.zeros", return_value=MagicMock()),
    ):
        dev = _detect_best_device()
        assert dev.type == "mps"


def test_detect_best_device_mps_failure_fallback():
    """Verify that _detect_best_device falls back to CPU when MPS probe fails."""
    with (
        patch("torch.cuda.is_available", return_value=False),
        patch.object(torch.backends.mps, "is_available", return_value=True, create=True),
        patch("torch.zeros", side_effect=RuntimeError("MPS probe error")),
    ):
        dev = _detect_best_device()
        assert dev.type == "cpu"


@patch("transformers.AutoModel.from_pretrained")
def test_local_embedder_gpu_load_fallback_to_cpu(mock_from_pretrained):
    """Verify LocalEmbedder falls back to CPU if moving to GPU fails during model load."""
    import zocrates.rag.embeddings as emb_module
    emb_module._SHARED_LOCAL_MODEL = None
    emb_module._SHARED_LOCAL_DEVICE = None

    mock_model = MagicMock()
    mock_model.config.hidden_size = 768

    def mock_to(device=None, dtype=None, **kwargs):
        if hasattr(device, "type") and device.type == "cuda" or device == "cuda":
            raise RuntimeError("CUDA out of memory during load")
        return mock_model

    mock_model.to.side_effect = mock_to
    mock_from_pretrained.return_value = mock_model

    with patch("zocrates.rag.embeddings._detect_best_device", return_value=torch.device("cuda")):
        embedder = LocalEmbedder()
        model = embedder._load()

        assert embedder._device.type == "cpu"
        assert model is mock_model


def test_local_embedder_encode_runtime_fallback_to_cpu():
    """Verify LocalEmbedder catches runtime GPU inference error, moves to CPU, and re-encodes."""
    embedder = LocalEmbedder()
    embedder._device = torch.device("cuda")

    mock_model = MagicMock()
    mock_model.config.hidden_size = 768
    mock_model.to.return_value = mock_model

    calls = 0

    def mock_encode(texts, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("CUDA runtime kernel error")
        return np.ones((len(texts), 768))

    mock_model.encode.side_effect = mock_encode

    res = embedder._encode_internal(mock_model, ["sample text"], task="retrieval", prompt_name="query")

    assert calls == 2  # First on GPU (failed), second on CPU (succeeded)
    assert embedder._device.type == "cpu"
    assert len(res) == 1
    assert len(res[0]) == 768
