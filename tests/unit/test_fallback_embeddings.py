"""Unit tests for simplified local CPU embedder initialization."""

from unittest.mock import MagicMock, patch
import pytest
import torch

from zocrates.rag.embeddings import LocalEmbedder, OpenRouterEmbedder
from zocrates.rag.hardware import _detect_best_device


def test_detect_best_device_fallback():
    """Verify that _detect_best_device falls back to a CPU torch device when no GPU accelerator is available."""
    with patch("torch.cuda.is_available", return_value=False), \
         patch.object(torch.backends, "mps", create=True) as mock_mps:
        mock_mps.is_available.return_value = False
        device = _detect_best_device()
        assert device.type == "cpu"


@patch("transformers.AutoModel.from_pretrained")
def test_local_embedder_cpu_loading(mock_from_pretrained):
    """Verify LocalEmbedder lazy-loads the Jina model on CPU with correct defaults.

    Mocks ``from_pretrained`` to avoid downloading real weights. Asserts that
    the model name and dimension are set to the expected Jina v5 nano values
    and that ``from_pretrained`` is called exactly once on first load.
    """
    import zocrates.rag.embeddings as emb_module
    emb_module._SHARED_LOCAL_MODEL = None

    mock_model = MagicMock()
    mock_model.config.hidden_size = 768
    mock_model.to.return_value = mock_model
    mock_from_pretrained.return_value = mock_model

    embedder = LocalEmbedder()
    model = embedder._load()

    assert embedder._model_name == "jinaai/jina-embeddings-v5-text-nano"
    assert embedder.dimension == 768
    mock_from_pretrained.assert_called_once()


def test_openrouter_embedder_sanitizes_data_image_uris():
    """Verify OpenRouterEmbedder sanitizes data:image/ URIs to prevent HTTP 400 VLM errors."""
    with (
        patch("zocrates.config.get_secure_api_key", return_value="test_openrouter_key"),
        patch("litellm.embedding") as mock_embedding,
    ):
        mock_embedding.return_value = MagicMock(data=[{"embedding": [0.1, 0.2]}, {"embedding": [0.3, 0.4]}])
        embedder = OpenRouterEmbedder(model_name="openrouter/nvidia/nemotron-3-embed-1b:free", dimension=2)

        input_texts = [
            "Research summary with [![Logo](data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAA)](https://example.com)",
            "Plain academic paragraph with no embedded image URIs.",
        ]

        res = embedder(input_texts)

        assert len(res) == 2
        mock_embedding.assert_called_once()
        passed_input = mock_embedding.call_args.kwargs["input"]

        # Ensure no data:image/ token exists in any passed text
        for item in passed_input:
            assert "data:image/" not in item

        # Verify replacement with [image]
        assert "[image]" in passed_input[0]
        assert "data:image/png;base64" not in passed_input[0]
        # Verify plain text is untouched
        assert passed_input[1] == input_texts[1]


@pytest.mark.parametrize("corrupted_dim", [1024, 9999])
@patch("transformers.AutoModel.from_pretrained")
def test_local_embedder_heals_corrupted_config_dimension(mock_from_pretrained, corrupted_dim):
    """Verify LocalEmbedder heals a corrupted config dimension (e.g. 1024 or 9999) back to 768 on load."""
    import zocrates.rag.embeddings as emb_module
    emb_module._SHARED_LOCAL_MODEL = None

    mock_model = MagicMock()
    mock_model.config.hidden_size = 768
    mock_model.to.return_value = mock_model
    mock_from_pretrained.return_value = mock_model

    with (
        patch("zocrates.config.load_zocrates_config", return_value={"embedding_dimension": corrupted_dim}),
        patch("zocrates.config.save_zocrates_config") as mock_save,
    ):
        embedder = LocalEmbedder()
        embedder._load()

        assert embedder.dimension == 768
        mock_save.assert_called_once_with({"embedding_dimension": 768})


def test_openrouter_embedder_clamps_dimension_exceeding_4096(caplog):
    """Verify OpenRouterEmbedder clamps configured dimension exceeding 4096 to 4096."""
    with patch("zocrates.config.load_zocrates_config", return_value={}):
        embedder = OpenRouterEmbedder(dimension=5000)
        assert embedder.dimension == 4096
        assert "exceeds ceiling 4096. Clamping to 4096." in caplog.text


def test_openrouter_embedder_clamps_config_dimension_exceeding_4096(caplog):
    """Verify OpenRouterEmbedder clamps dimension loaded from config exceeding 4096 to 4096."""
    with patch("zocrates.config.load_zocrates_config", return_value={"embedding_dimension": 8192}):
        embedder = OpenRouterEmbedder()
        assert embedder.dimension == 4096
        assert "exceeds ceiling 4096. Clamping to 4096." in caplog.text


def test_openrouter_embedder_call_dimension_exceeds_4096_warns_and_does_not_persist(caplog):
    """Verify OpenRouterEmbedder.__call__ logs warning when auto-detected dimension > 4096 and does not persist."""
    with (
        patch("zocrates.config.load_zocrates_config", return_value={}),
        patch("zocrates.config.get_secure_api_key", return_value="dummy_key"),
        patch("zocrates.config.save_zocrates_config") as mock_save,
        patch("litellm.embedding") as mock_embedding,
    ):
        mock_embedding.return_value = MagicMock(data=[{"embedding": [0.1] * 5000}])
        embedder = OpenRouterEmbedder(model_name="openrouter/test-model")
        res = embedder(["sample text"])
        assert len(res) == 1
        assert len(res[0]) == 5000
        assert "Returned embedding dimension 5000 exceeds ceiling 4096." in caplog.text
        mock_save.assert_not_called()


def test_update_embedding_model_rejects_dimension_exceeding_ceiling():
    """Verify POST /rag/model rejects dimension > 4096 with HTTP 400."""
    from fastapi.testclient import TestClient
    from zocrates.config import get_auth_token
    from zocrates.fast_api_app import app

    client = TestClient(app, headers={"Authorization": f"Bearer {get_auth_token()}"})
    response = client.post(
        "/rag/model",
        json={"dimension": 5000},
    )
    assert response.status_code == 400
    assert "Embedding dimension must be between 64 and 4096." in response.json()["detail"]


def test_update_embedding_model_rejects_dimension_below_floor():
    """Verify POST /rag/model rejects dimension < 64 with HTTP 400."""
    from fastapi.testclient import TestClient
    from zocrates.config import get_auth_token
    from zocrates.fast_api_app import app

    client = TestClient(app, headers={"Authorization": f"Bearer {get_auth_token()}"})
    response = client.post(
        "/rag/model",
        json={"dimension": 32},
    )
    assert response.status_code == 400
    assert "Embedding dimension must be between 64 and 4096." in response.json()["detail"]


def test_update_embedding_model_local_rejects_non_768_dimension(monkeypatch):
    """Verify POST /rag/model in local mode rejects dimension other than 768 with HTTP 400 without mutating env."""
    import os
    from fastapi.testclient import TestClient
    from zocrates.config import get_auth_token
    from zocrates.fast_api_app import app

    monkeypatch.setenv("EMBEDDING_MODE", "cloud")
    client = TestClient(app, headers={"Authorization": f"Bearer {get_auth_token()}"})
    with patch("zocrates.config.load_zocrates_config", return_value={"embedding_provider": "local"}):
        response = client.post(
            "/rag/model",
            json={"mode": "local", "dimension": 1024},
        )
        assert response.status_code == 400
        assert "Local Jina embeddings model only supports dimension 768." in response.json()["detail"]
        # Ensure EMBEDDING_MODE was NOT mutated on rejected request
        assert os.environ.get("EMBEDDING_MODE") == "cloud"


