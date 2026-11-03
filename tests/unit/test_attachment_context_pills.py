"""Unit tests for Attachment Context Pills (Phase 3 backend implementation)."""

import base64
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from google.genai import types
from pydantic import ValidationError

from zocrates.agents.common.compaction import FailOpenLlmEventSummarizer
from zocrates.agents.common.prompts import strip_snippet_keys
from zocrates.agents.coordinator.agent import create_coordinator_agent
from zocrates.agents.coordinator.prompts import (
    COORDINATOR_INSTRUCTION,
    COORDINATOR_INSTRUCTION_dynamic,
    get_coordinator_instruction,
)
from zocrates.agents.library_search.prompts import LIBRARY_SEARCH_AGENT_INSTRUCTION
from zocrates.agents.library_search.search_pipeline import search_pipeline_node
from google.adk.agents.readonly_context import ReadonlyContext
from zocrates.config import get_auth_token
from zocrates.fast_api_app import _process_request_snippets, app
from zocrates.llm import is_model_vision_capable
from zocrates.schemas.chat_request import ChatRequest
from zocrates.schemas.item_request import ActiveItem, SnippetItem
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def reset_sse_app_status():
    """Reset sse_starlette AppStatus should_exit_event before each test."""
    import sse_starlette.sse

    sse_starlette.sse.AppStatus.should_exit_event = None
    yield
    sse_starlette.sse.AppStatus.should_exit_event = None


@pytest.fixture
def client():
    return TestClient(app, headers={"Authorization": f"Bearer {get_auth_token()}"})


def test_snippet_item_camel_case_parsing():
    """Verify SnippetItem accepts camelCase keys from front-end payloads."""
    payload = {
        "id": "snip_1",
        "type": "text",
        "itemKey": "ITEM123",
        "parentKey": "PARENT456",
        "paperTitle": "Attention Is All You Need",
        "page": 3,
        "text": "Self-attention mechanism",
    }
    item = SnippetItem.model_validate(payload)
    assert item.id == "snip_1"
    assert item.type == "text"
    assert item.item_key == "ITEM123"
    assert item.parent_key == "PARENT456"
    assert item.paper_title == "Attention Is All You Need"
    assert item.page == 3
    assert item.text == "Self-attention mechanism"
    assert item.mime_type == "image/jpeg"


def test_snippet_item_snake_case_parsing():
    """Verify SnippetItem accepts standard snake_case keys."""
    payload = {
        "id": "snip_2",
        "type": "image",
        "item_key": "ITEM789",
        "parent_key": "PARENT789",
        "paper_title": "Deep Residual Learning",
        "page": 5,
        "image_data": "data:image/jpeg;base64,ZmFrZWltYWdl",
        "mime_type": "image/jpeg",
    }
    item = SnippetItem.model_validate(payload)
    assert item.id == "snip_2"
    assert item.type == "image"
    assert item.image_data == "data:image/jpeg;base64,ZmFrZWltYWdl"
    assert item.mime_type == "image/jpeg"


def test_snippet_item_image_data_max_length_guard():
    """Verify oversized image_data payloads (>5,000,000 chars) are rejected."""
    oversized = "a" * 5_000_001
    with pytest.raises(ValidationError):
        SnippetItem(id="snip_err", type="image", image_data=oversized)


def test_active_item_key_aliases():
    """Verify ActiveItem accepts key, item_key, and itemKey aliases."""
    item1 = ActiveItem.model_validate({"key": "K1", "title": "Paper 1"})
    assert item1.key == "K1"
    assert item1.title == "Paper 1"

    item2 = ActiveItem.model_validate({"item_key": "K2", "title": "Paper 2"})
    assert item2.key == "K2"
    assert item2.title == "Paper 2"

    item3 = ActiveItem.model_validate({"itemKey": "K3", "title": "Paper 3"})
    assert item3.key == "K3"
    assert item3.title == "Paper 3"

    item4 = ActiveItem(key="K4", title="Paper 4")
    assert item4.key == "K4"


def test_snippet_item_string_page_and_2mb_payload():
    """Verify SnippetItem accepts string pages, generates default id, and allows 2MB image payload."""
    payload = {
        "type": "image",
        "page": "14",
        "imageData": "data:image/jpeg;base64," + ("a" * 2_000_000),
    }
    item = SnippetItem.model_validate(payload)
    assert item.page == "14"
    assert item.id.startswith("snip_")
    assert item.image_data is not None
    assert len(item.image_data) > 2_000_000

    # Also test integer page
    item_int_page = SnippetItem.model_validate({"type": "text", "page": 42})
    assert item_int_page.page == 42


def test_validation_error_handler_readable_detail(client):
    """Verify 422 RequestValidationError handler returns human-readable detail string and structured errors."""
    resp = client.post("/chat", json={})
    assert resp.status_code == 422
    data = resp.json()
    assert "detail" in data
    assert isinstance(data["detail"], str)
    assert "prompt" in data["detail"]
    assert "Field required" in data["detail"]
    assert "errors" in data
    assert isinstance(data["errors"], list)
    assert len(data["errors"]) > 0

    resp2 = client.post(
        "/chat",
        json={
            "prompt": "Test prompt",
            "session_id": "sess_1",
            "snippets": [{"type": "invalid_type"}],
        },
    )
    assert resp2.status_code == 422
    data2 = resp2.json()
    assert isinstance(data2["detail"], str)
    assert "body -> snippets -> 0 -> type" in data2["detail"]
    assert isinstance(data2["errors"], list)


def test_chat_request_with_snippets():
    """Verify ChatRequest parses snippets list properly."""
    payload = {
        "prompt": "Explain this equation",
        "session_id": "sess_123",
        "snippets": [
            {
                "id": "snip_1",
                "type": "text",
                "text": "E = mc^2",
                "paperTitle": "Relativity",
                "page": 1,
            }
        ],
    }
    req = ChatRequest.model_validate(payload)
    assert req.prompt == "Explain this equation"
    assert req.snippets is not None
    assert len(req.snippets) == 1
    assert req.snippets[0].text == "E = mc^2"
    assert req.snippets[0].paper_title == "Relativity"


def test_is_model_vision_capable():
    """Verify vision detection for various model naming conventions."""
    assert is_model_vision_capable("google/gemini-2.0-flash-exp:free") is True
    assert is_model_vision_capable("gemini-1.5-pro") is True
    assert is_model_vision_capable("openai/gpt-4o") is True
    assert is_model_vision_capable("openai/gpt-4.5-preview") is True
    assert is_model_vision_capable("openai/gpt-5-turbo") is True
    assert is_model_vision_capable("openai/gpt-6") is True
    assert is_model_vision_capable("openai/gpt-6-mini") is True
    assert is_model_vision_capable("gpt-4-turbo") is True
    assert is_model_vision_capable("openai/o1") is True
    assert is_model_vision_capable("openai/o3") is True
    assert is_model_vision_capable("openai/o4") is True
    assert is_model_vision_capable("openai/o6-preview") is True
    assert is_model_vision_capable("anthropic/claude-3-5-sonnet") is True
    assert is_model_vision_capable("anthropic/claude-3-7-sonnet") is True
    assert is_model_vision_capable("claude-4-opus") is True
    assert is_model_vision_capable("claude-5-sonnet") is True
    assert is_model_vision_capable("anthropic/claude-sonnet-5-5") is True
    assert is_model_vision_capable("qwen/qwen-2.5-vl-72b-instruct") is True
    assert is_model_vision_capable("qwen-vl-plus") is True
    assert is_model_vision_capable("deepseek/deepseek-vl2") is True
    assert is_model_vision_capable("allenai/molmo-7b") is True
    assert is_model_vision_capable("openbmb/minicpm-v-2_6") is True
    assert is_model_vision_capable("pixtral-12b") is True
    assert is_model_vision_capable("ollama/llava:latest") is True
    assert is_model_vision_capable("meta-llama/llama-3.2-11b-vision-instruct") is True
    assert is_model_vision_capable("meta-llama/llama-4-vision") is True

    # Gemma 3/4, PaliGemma, Qwen VL generations, Phi multimodal, DeepSeek-VL
    assert is_model_vision_capable("google/gemma-3-4b-it") is True
    assert is_model_vision_capable("gemma-3:12b") is True
    assert is_model_vision_capable("gemma-4:31b") is True
    assert is_model_vision_capable("gemma-5-flash") is True
    assert is_model_vision_capable("gemma3n") is True
    assert is_model_vision_capable("paligemma") is True
    assert is_model_vision_capable("paligemma2-10b") is True
    assert is_model_vision_capable("qwen2.5vl") is True
    assert is_model_vision_capable("qwen3vl") is True
    assert is_model_vision_capable("qwen3-vl:8b") is True
    assert is_model_vision_capable("qwen4vl") is True
    assert is_model_vision_capable("deepseek-vl2") is True
    assert is_model_vision_capable("deepseek-vision") is True
    assert is_model_vision_capable("phi4vl") is True
    assert is_model_vision_capable("phi-4-multimodal-instruct") is True
    assert is_model_vision_capable("mistral-pixtral-12b") is True
    assert is_model_vision_capable("mistral-vision") is True

    # GLM vision models, Grok, and Amazon Nova
    assert is_model_vision_capable("openrouter/z-ai/glm-5.3-flash") is True
    assert is_model_vision_capable("z-ai/glm-5.3-flash") is True
    assert is_model_vision_capable("z-ai/glm-5.3-flashx") is True
    assert is_model_vision_capable("z-ai/glm-5v-turbo") is True
    assert is_model_vision_capable("x-ai/grok-4.7") is True
    assert is_model_vision_capable("amazon/nova-pro-v1") is True

    # Text-only models
    assert is_model_vision_capable("gemma-3:1b") is False
    assert is_model_vision_capable("gemma-3:270m") is False
    assert is_model_vision_capable("z-ai/glm-5.3") is False
    assert is_model_vision_capable("z-ai/glm-4.7") is False
    assert is_model_vision_capable("openai/o3-mini") is False
    assert is_model_vision_capable("deepseek/deepseek-chat") is False
    assert is_model_vision_capable("deepseek/deepseek-r1") is False
    assert is_model_vision_capable("meta-llama/llama-3-70b-instruct") is False
    assert is_model_vision_capable("mistralai/mistral-large") is False
    assert is_model_vision_capable("anthropic/claude-2.1") is False
    assert is_model_vision_capable("") is False


def test_is_model_vision_capable_tier1_catalog_lookup(tmp_path):
    """Verify Tier 1 authoritative OpenRouter catalog lookup from memory and disk cache."""
    import json
    from unittest.mock import patch

    # 1. Authoritative disk cache lookup
    cache_dir = tmp_path / "openrouter_models_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    catalog = {
        "timestamp": 1234567890,
        "data": [
            {
                "id": "custom-vendor/vision-model",
                "architecture": {
                    "modality": "text+image->text",
                    "input_modalities": ["text", "image"],
                },
            },
            {
                "id": "custom-vendor/text-only-model",
                "architecture": {
                    "modality": "text->text",
                    "input_modalities": ["text"],
                },
            },
            {
                # Even with 'vlm' in the name, catalog explicitly says text-only
                "id": "custom-vendor/vlm-in-name-text-only",
                "architecture": {
                    "modality": "text->text",
                    "input_modalities": ["text"],
                },
            },
        ],
    }
    with open(cache_dir / "llm_models.json", "w", encoding="utf-8") as f:
        json.dump(catalog, f)

    with patch("zocrates.llm._OPENROUTER_LLM_CACHE", {"timestamp": 0, "data": []}):
        # Disk cache lookup
        assert is_model_vision_capable("custom-vendor/vision-model", zotero_data_dir=tmp_path) is True
        assert is_model_vision_capable("openrouter/custom-vendor/vision-model", zotero_data_dir=tmp_path) is True
        assert is_model_vision_capable("custom-vendor/text-only-model", zotero_data_dir=tmp_path) is False
        # Authoritative catalog classification overrides Tier 2 keyword match
        assert is_model_vision_capable("custom-vendor/vlm-in-name-text-only", zotero_data_dir=tmp_path) is False

    # 2. Authoritative in-memory cache lookup
    mem_cache = {
        "timestamp": 1234567890,
        "data": [
            {
                "id": "mem-vendor/mem-vision",
                "input_modalities": ["text", "image"],
            },
            {
                "id": "mem-vendor/mem-text",
                "input_modalities": ["text"],
            },
        ],
    }
    with patch("zocrates.llm._OPENROUTER_LLM_CACHE", mem_cache):
        assert is_model_vision_capable("mem-vendor/mem-vision") is True
        assert is_model_vision_capable("mem-vendor/mem-text") is False


@pytest.mark.asyncio
async def test_process_request_snippets_none_or_empty():
    """Verify empty or None snippets return empty structures."""
    parts, text, notices = await _process_request_snippets(None, "deepseek/deepseek-chat", None)
    assert parts == []
    assert text == ""
    assert notices == []

    parts, text, notices = await _process_request_snippets([], "deepseek/deepseek-chat", None)
    assert parts == []
    assert text == ""
    assert notices == []


@pytest.mark.asyncio
async def test_process_request_snippets_text_snippet():
    """Verify text snippet formats into XML block without multimodal parts."""
    snips = [
        SnippetItem(
            id="s1",
            type="text",
            text="Attention is all you need for sequence modeling.",
            paper_title="Attention Is All You Need",
            page=2,
            item_key="ATTN1",
            parent_key="PARENT_ATTN",
        )
    ]
    parts, text, notices = await _process_request_snippets(snips, "deepseek/deepseek-chat", None)
    assert parts == []
    assert notices == []
    assert "# Context: User-Selected Focal Snippets from Attachment" in text
    assert '<text_snippet paper="Attention Is All You Need" page="2" key="PARENT_ATTN">' in text
    assert "Attention is all you need for sequence modeling." in text


@pytest.mark.asyncio
async def test_process_request_snippets_image_vision_model():
    """Verify image snippet with vision model attaches Part.from_bytes."""
    raw_png = b"\x89PNG\r\n\x1a\nfakeimagebytes"
    b64_png = base64.b64encode(raw_png).decode("ascii")
    snips = [
        SnippetItem(
            id="s2",
            type="image",
            image_data=f"data:image/png;base64,{b64_png}",
            paper_title="ResNet",
            page=4,
            item_key="RES1",
        )
    ]
    parts, text, notices = await _process_request_snippets(snips, "google/gemini-2.0-flash-exp:free", None)
    assert len(parts) == 1
    assert getattr(parts[0], "inline_data", None) is not None
    assert parts[0].inline_data.data == raw_png
    assert parts[0].inline_data.mime_type == "image/png"
    assert notices == []
    assert '<image_snippet paper="ResNet" page="4" key="RES1">' in text
    assert "[Attached visual image of selected area]" in text


@pytest.mark.asyncio
async def test_process_request_snippets_image_text_model_untranscribed_immediately():
    """Verify text-only model immediately receives untranscribed image snippet and notice without VLM dependencies."""
    raw_jpg = b"\xff\xd8\xfffakejpegbytes"
    b64_jpg = base64.b64encode(raw_jpg).decode("ascii")
    snips = [
        SnippetItem(
            id="s3",
            type="image",
            image_data=f"data:image/jpeg;base64,{b64_jpg}",
            paper_title="BERT",
            page=5,
        )
    ]

    parts, text, notices = await _process_request_snippets(snips, "deepseek/deepseek-chat", None)
    assert parts == []
    assert len(notices) == 1
    assert notices[0]["notice"] == "Image area attached, but active model (deepseek/deepseek-chat) is text-only. The agent has been notified."
    assert '<image_snippet paper="BERT" page="5" key="">' in text
    assert "[Attached image area: untranscribed image on page 5. The active coordinator model (deepseek/deepseek-chat) does not support vision/image inputs. If needed, inspect the surrounding document text or section via retrieval tools.]" in text


@pytest.mark.asyncio
async def test_process_request_snippets_ollama_text_only_model_immediate_notice():
    """Verify text-only Ollama model immediately receives untranscribed image snippet and notice without VLM provider checks."""
    raw_jpg = b"\xff\xd8\xfffakejpegbytes"
    b64_jpg = base64.b64encode(raw_jpg).decode("ascii")
    snips = [
        SnippetItem(
            id="s_ollama_text",
            type="image",
            image_data=f"data:image/jpeg;base64,{b64_jpg}",
            paper_title="Ollama Text Only",
            page=3,
        )
    ]

    parts, text, notices = await _process_request_snippets(snips, "ollama/llama3.1:8b", None)
    assert parts == []
    assert len(notices) == 1
    assert notices[0]["notice"] == "Image area attached, but active model (ollama/llama3.1:8b) is text-only. The agent has been notified."
    assert '<image_snippet paper="Ollama Text Only" page="3" key="">' in text
    assert "[Attached image area: untranscribed image on page 3. The active coordinator model (ollama/llama3.1:8b) does not support vision/image inputs. If needed, inspect the surrounding document text or section via retrieval tools.]" in text


@pytest.mark.asyncio
async def test_process_request_snippets_text_model_with_author_and_item_key():
    """Verify text-only model includes author string and item_key in untranscribed block."""
    raw_jpg = b"\xff\xd8\xfffakejpegbytes"
    b64_jpg = base64.b64encode(raw_jpg).decode("ascii")
    snips = [
        SnippetItem(
            id="s4",
            type="image",
            image_data=f"data:image/jpeg;base64,{b64_jpg}",
            paper_title="Transformer",
            author_str="Vaswani et al.",
            page=6,
            item_key="TRANS1",
        )
    ]

    parts, text, notices = await _process_request_snippets(snips, "deepseek/deepseek-chat", None)
    assert parts == []
    assert len(notices) == 1
    assert notices[0]["notice"] == "Image area attached, but active model (deepseek/deepseek-chat) is text-only. The agent has been notified."
    assert '<image_snippet paper="Transformer" authors="Vaswani et al." page="6" key="TRANS1">' in text
    assert "[Attached image area: untranscribed image on page 6. The active coordinator model (deepseek/deepseek-chat) does not support vision/image inputs. If needed, inspect the surrounding document text or section via retrieval tools.]" in text


@pytest.mark.asyncio
async def test_process_request_snippets_text_model_parent_key_priority():
    """Verify parent_key takes priority over item_key in untranscribed block for text-only model."""
    raw_jpg = b"\xff\xd8\xfffakejpegbytes"
    b64_jpg = base64.b64encode(raw_jpg).decode("ascii")
    snips = [
        SnippetItem(
            id="s_parent",
            type="image",
            image_data=f"data:image/jpeg;base64,{b64_jpg}",
            paper_title="BERT",
            author_str="Devlin et al.",
            page=2,
            item_key="CHILD_KEY",
            parent_key="PARENT_KEY",
        )
    ]

    parts, text, notices = await _process_request_snippets(snips, "deepseek/deepseek-chat", None)
    assert parts == []
    assert len(notices) == 1
    assert '<image_snippet paper="BERT" authors="Devlin et al." page="2" key="PARENT_KEY">' in text


@pytest.mark.asyncio
async def test_process_request_snippets_text_model_missing_page_and_title():
    """Verify fallback defaults for missing page ('?') and title ('Unknown') for text-only model."""
    raw_jpg = b"\xff\xd8\xfffakejpegbytes"
    b64_jpg = base64.b64encode(raw_jpg).decode("ascii")
    snips = [
        SnippetItem(
            id="s_missing",
            type="image",
            image_data=f"data:image/jpeg;base64,{b64_jpg}",
            paper_title=None,
            page=None,
        )
    ]

    parts, text, notices = await _process_request_snippets(snips, "deepseek/deepseek-chat", None)
    assert parts == []
    assert len(notices) == 1
    assert '<image_snippet paper="Unknown" page="?" key="">' in text
    assert "[Attached image area: untranscribed image on page ?. The active coordinator model (deepseek/deepseek-chat) does not support vision/image inputs. If needed, inspect the surrounding document text or section via retrieval tools.]" in text


@pytest.mark.asyncio
async def test_process_request_snippets_multiple_images_text_only_model():
    """Verify multiple image snippets with text model each receive untranscribed blocks and notices."""
    raw_jpg = b"\xff\xd8\xfffakejpegbytes"
    b64_jpg = base64.b64encode(raw_jpg).decode("ascii")
    snips = [
        SnippetItem(
            id="s_multi_1",
            type="image",
            image_data=f"data:image/jpeg;base64,{b64_jpg}",
            paper_title="Paper Alpha",
            page=1,
            item_key="KEY_A",
        ),
        SnippetItem(
            id="s_multi_2",
            type="image",
            image_data=f"data:image/jpeg;base64,{b64_jpg}",
            paper_title="Paper Beta",
            page=2,
            item_key="KEY_B",
        ),
    ]

    parts, text, notices = await _process_request_snippets(snips, "deepseek/deepseek-chat", None)
    assert parts == []
    assert len(notices) == 2
    assert '<image_snippet paper="Paper Alpha" page="1" key="KEY_A">' in text
    assert '<image_snippet paper="Paper Beta" page="2" key="KEY_B">' in text


def test_coordinator_prompts_inclusion():
    """Verify COORDINATOR_INSTRUCTION and COORDINATOR_INSTRUCTION_dynamic contain focal snippet rules."""
    assert "unless the figure or area is already provided inline as an attached image snippet in this conversation" in COORDINATOR_INSTRUCTION
    assert "Focal Snippets from Attachments (Adaptive Soft Dispatch)" in COORDINATOR_INSTRUCTION
    assert "No Fabricated Numeric Citations" in COORDINATOR_INSTRUCTION
    assert "Image Snippets & Vision Inspection" in COORDINATOR_INSTRUCTION
    assert "untranscribed because the model is text-only" in COORDINATOR_INSTRUCTION
    assert "{focal_snippets_str?}" in COORDINATOR_INSTRUCTION_dynamic
    assert "Search subagents already receive the focal snippet context directly." in COORDINATOR_INSTRUCTION
    assert "if delegating to `search_online_literature`" in COORDINATOR_INSTRUCTION
    assert "without repeating keys or copying the full excerpt" in COORDINATOR_INSTRUCTION
    assert "describe what the image depicts in the 'request' parameter" in COORDINATOR_INSTRUCTION


def test_strip_snippet_keys_removes_keys_from_text_and_image_tags():
    """Verify strip_snippet_keys programmatically removes key attributes while keeping other metadata."""
    # 1. Text snippet with key, authors, paper, page
    text_snip = (
        '<text_snippet paper="Attention Is All You Need" authors="Vaswani et al." page="2" key="ATTN_123">\n'
        'Self-attention layers map queries and keys to outputs.\n'
        '</text_snippet>'
    )
    stripped_text = strip_snippet_keys(text_snip)
    assert 'key="ATTN_123"' not in stripped_text
    assert 'key=' not in stripped_text
    assert 'paper="Attention Is All You Need"' in stripped_text
    assert 'authors="Vaswani et al."' in stripped_text
    assert 'page="2"' in stripped_text
    assert 'Self-attention layers map queries and keys to outputs.' in stripped_text

    # 2. Image snippet with key
    img_snip = (
        '<image_snippet paper="ResNet" page="5" key="RES_456">\n'
        'Residual block diagram with skip connection.\n'
        '</image_snippet>'
    )
    stripped_img = strip_snippet_keys(img_snip)
    assert 'key="RES_456"' not in stripped_img
    assert 'key=' not in stripped_img
    assert 'paper="ResNet"' in stripped_img
    assert 'page="5"' in stripped_img
    assert 'Residual block diagram with skip connection.' in stripped_img

    # 3. Untranscribed visual image snippet with key
    visual_snip = (
        '<image_snippet paper="Transformers" page="3" key="VIS_789">\n'
        '[Attached visual image of selected area]\n'
        '</image_snippet>'
    )
    stripped_visual = strip_snippet_keys(visual_snip)
    assert 'key="VIS_789"' not in stripped_visual
    assert 'key=' not in stripped_visual
    assert 'paper="Transformers"' in stripped_visual
    assert '[Attached visual image of selected area]' in stripped_visual

    # 4. Empty or None strings
    assert strip_snippet_keys("") == ""
    assert strip_snippet_keys(None) == ""


@pytest.mark.asyncio
async def test_coordinator_context_free_of_item_keys():
    """Verify coordinator dynamic instruction strips internal Zotero item keys."""
    raw_snippets = (
        "\n\n# Context: User-Selected Focal Snippets from Attachment\n\n"
        '<text_snippet paper="Convergence Proofs" page="4" key="KEY_TEXT_001">\n'
        'Theorem 2 holds asymptotically.\n'
        '</text_snippet>\n\n'
        '<image_snippet paper="Convergence Proofs" page="5" key="KEY_IMG_002">\n'
        'A scatter plot of convergence rates.\n'
        '</image_snippet>\n\n'
    )

    # Test get_coordinator_instruction directly
    state_dict = {
        "user_research_profile_str": "# User Research Profile & Directives\nBe precise.\n\n",
        "active_context_items_str": '- Title: "Convergence Proofs" (2021)',
        "focal_snippets_str": raw_snippets,
    }
    instruction_text = get_coordinator_instruction(state_dict)

    assert "# Context: User-Selected Focal Snippets from Attachment" in instruction_text
    assert "# Context: Active Items Selected in Zotero" in instruction_text
    assert "# User Research Profile & Directives" in instruction_text
    assert "Theorem 2 holds asymptotically." in instruction_text
    assert "A scatter plot of convergence rates." in instruction_text

    # Assert completely free of internal item keys
    assert "KEY_TEXT_001" not in instruction_text
    assert "KEY_IMG_002" not in instruction_text
    assert "key=" not in instruction_text

    # Test through create_coordinator_agent and canonical_instruction
    coord = create_coordinator_agent()
    mock_inv = MagicMock()
    mock_inv.session.state = state_dict
    ctx = ReadonlyContext(mock_inv)

    resolved_instruction, bypass = await coord.canonical_instruction(ctx)
    assert bypass is True
    assert "# Context: User-Selected Focal Snippets from Attachment" in resolved_instruction
    assert "KEY_TEXT_001" not in resolved_instruction
    assert "KEY_IMG_002" not in resolved_instruction
    assert "key=" not in resolved_instruction


def test_coordinator_guideline_rule_8_subagent_propagation():
    """Verify COORDINATOR_INSTRUCTION Guideline Rule 8 specifies snippet propagation rules."""
    assert "Focal Snippets from Attachments (Adaptive Soft Dispatch)" in COORDINATOR_INSTRUCTION
    assert "# Context: User-Selected Focal Snippets from Attachment" in COORDINATOR_INSTRUCTION
    assert "Search subagents already receive the focal snippet context directly." in COORDINATOR_INSTRUCTION
    assert "if delegating to `search_online_literature`" in COORDINATOR_INSTRUCTION
    assert "without repeating keys or copying the full excerpt" in COORDINATOR_INSTRUCTION
    assert "describe what the image depicts in the 'request' parameter" in COORDINATOR_INSTRUCTION


@pytest.mark.asyncio
async def test_search_pipeline_subagent_retains_keys_and_synthesizer_strips_keys():
    """Verify search subagent retains keys in node input while synthesizer strips keys."""
    raw_snippets = (
        '<text_snippet paper="Attention Is All You Need" page="3" key="ATTN_INTERNAL_KEY">\n'
        'Multi-Head Attention computes queries and keys.\n'
        '</text_snippet>'
    )

    class MockWorkflowCtx:
        def __init__(self, state):
            self.state = state
            self.run_node = AsyncMock(return_value="Synthesized literature review")
            self._invocation_context = MagicMock()
            self.session = None

    ctx = MockWorkflowCtx({
        "focal_snippets_str": raw_snippets,
        "user_prompt": "Explain multi-head attention",
    })

    with patch("zocrates.agents.library_search.search_pipeline.rerank_evidence", new_callable=AsyncMock) as m_rerank:
        m_rerank.return_value = []
        await search_pipeline_node._func(ctx, "Explain multi-head attention")

    assert ctx.run_node.call_count >= 2

    # Call 0 is search_initial (library_search_agent)
    search_call = ctx.run_node.call_args_list[0]
    search_input = search_call.kwargs.get("node_input", "")
    assert "# Context: User-Selected Focal Snippets from Attachment" in search_input
    assert 'key="ATTN_INTERNAL_KEY"' in search_input
    assert "Multi-Head Attention computes queries and keys." in search_input

    # Last call is synth (library_synthesizer_agent)
    synth_call = ctx.run_node.call_args_list[-1]
    synth_input = synth_call.kwargs.get("node_input", "")
    assert "# Context: User-Selected Focal Snippets from Attachment" in synth_input
    assert "Multi-Head Attention computes queries and keys." in synth_input
    # Internal key MUST be stripped from synthesizer context
    assert 'key="ATTN_INTERNAL_KEY"' not in synth_input
    assert "ATTN_INTERNAL_KEY" not in synth_input
    assert "key=" not in synth_input


def test_compaction_inline_data_handling():
    """Verify FailOpenLlmEventSummarizer preserves inline_data as a clean text marker."""
    summarizer = FailOpenLlmEventSummarizer(llm=MagicMock())

    # Create dummy Event with inline image part
    image_part = types.Part.from_bytes(data=b"fake", mime_type="image/jpeg")
    text_part = types.Part.from_text(text="What is this diagram?")

    mock_event = MagicMock()
    mock_event.author = "user"
    mock_event.content = types.Content(role="user", parts=[text_part, image_part])

    summary_str = summarizer.format_events_for_summary([mock_event])
    assert "**User:**\nWhat is this diagram?" in summary_str
    assert "**User Attached:** [Visual Image Snippet (image/jpeg)]" in summary_str
    assert "b64" not in summary_str
    assert "fake" not in summary_str


def test_chat_endpoint_state_delta_with_snippets(client):
    """Verify /chat endpoint sets focal_snippets_str in state_delta and resets when absent."""
    captured_state_deltas = []

    async def fake_run_async(*args, **kwargs):
        captured_state_deltas.append(kwargs.get("state_delta"))
        mock_event = MagicMock()
        mock_event.id = "evt-1"
        mock_event.author = "agent"
        mock_event.is_final_response.return_value = True
        mock_event.content.parts = [MagicMock(text="Hello", thought=None, function_call=None, function_response=None)]
        yield mock_event

    mock_runner = MagicMock()
    mock_runner.run_async = fake_run_async
    mock_runner.session_service.get_session = AsyncMock(return_value=None)
    mock_syncer = MagicMock()

    with patch("zocrates.fast_api_app.get_syncer", return_value=mock_syncer):
        app.state.runner = mock_runner

        # Turn 1: With a text snippet
        res1 = client.post(
            "/chat",
            json={
                "prompt": "Explain theorem 2",
                "session_id": "test-sess-pills",
                "snippets": [
                    {
                        "id": "s1",
                        "type": "text",
                        "text": "Theorem 2 states that all representations converge.",
                        "paperTitle": "Convergence Proofs",
                        "page": 4,
                    }
                ],
            },
        )
        assert res1.status_code == 200
        assert len(captured_state_deltas) == 1
        sd1 = captured_state_deltas[0]
        assert "focal_snippets_str" in sd1
        assert '<text_snippet paper="Convergence Proofs" page="4"' in sd1["focal_snippets_str"]
        assert "Theorem 2 states that all representations converge." in sd1["focal_snippets_str"]

        # Turn 2: Follow-up without snippets -> invariant: resets to empty string
        import sse_starlette.sse
        sse_starlette.sse.AppStatus.should_exit_event = None

        res2 = client.post(
            "/chat",
            json={
                "prompt": "Can you give an intuition for why that holds?",
                "session_id": "test-sess-pills",
            },
        )
        assert res2.status_code == 200
        assert len(captured_state_deltas) == 2
        sd2 = captured_state_deltas[1]
        assert "focal_snippets_str" in sd2
        assert sd2["focal_snippets_str"] == ""


def test_chat_endpoint_notice_yielded_for_text_only_model(client):
    """Verify /chat yields notice payload as SSE event when active model is text-only."""
    async def fake_run_async(*args, **kwargs):
        mock_event = MagicMock()
        mock_event.id = "evt-2"
        mock_event.author = "agent"
        mock_event.is_final_response.return_value = True
        mock_event.content.parts = [MagicMock(text="Hello", thought=None, function_call=None, function_response=None)]
        yield mock_event

    mock_runner = MagicMock()
    mock_runner.run_async = fake_run_async
    mock_runner.session_service.get_session = AsyncMock(return_value=None)
    mock_syncer = MagicMock()

    raw_jpg = b"\xff\xd8\xfffakejpegbytes"
    b64_jpg = base64.b64encode(raw_jpg).decode("ascii")

    with (
        patch("zocrates.fast_api_app.get_syncer", return_value=mock_syncer),
        patch("zocrates.llm.is_model_vision_capable", return_value=False),
    ):
        app.state.runner = mock_runner

        res = client.post(
            "/chat",
            json={
                "prompt": "Describe this figure",
                "session_id": "test-sess-notice",
                "snippets": [
                    {
                        "id": "s_img",
                        "type": "image",
                        "imageData": f"data:image/jpeg;base64,{b64_jpg}",
                        "paperTitle": "Benchmark Paper",
                        "page": 7,
                    }
                ],
            },
        )
        assert res.status_code == 200
        # Check SSE response content contains the notice JSON
        body = res.text
        assert "notice" in body
        assert "Image area attached, but active model" in body
        assert "is text-only. The agent has been notified." in body


def test_chat_request_max_snippets_ceiling():
    """Verify ChatRequest rejects more than 20 snippets."""
    snips = [
        SnippetItem(id=f"s_{i}", type="text", text=f"text {i}")
        for i in range(21)
    ]
    with pytest.raises(ValidationError):
        ChatRequest(prompt="test", session_id="sess", snippets=snips)


@pytest.mark.asyncio
async def test_process_request_snippets_malformed_and_empty_images():
    """Verify graceful degradation when snippet image data is empty or malformed."""
    snips = [
        SnippetItem(
            id="s_empty",
            type="image",
            image_data=None,
            paper_title="Paper A",
            page=1,
        ),
        SnippetItem(
            id="s_malformed_uri",
            type="image",
            image_data="data:image/jpeg;notbase64!!!",
            paper_title="Paper B",
            page=2,
        ),
        SnippetItem(
            id="s_no_comma",
            type="image",
            image_data="data:image/jpeg;broken",
            paper_title="Paper C",
            page=3,
        ),
    ]

    parts, text, notices = await _process_request_snippets(snips, "google/gemini-2.0-flash-exp:free", None)
    assert parts == []
    assert notices == []
    assert "Attached image area payload was empty or could not be transferred." in text
    assert "Attached image area could not be decoded." in text


@pytest.mark.asyncio
async def test_process_request_snippets_text_only_preserves_parent_key():
    """Verify text-only model preserves parent_key and page attributes in untranscribed block."""
    raw_jpg = b"\xff\xd8\xfffakejpegbytes"
    b64_jpg = base64.b64encode(raw_jpg).decode("ascii")
    snips = [
        SnippetItem(
            id="s_parent",
            type="image",
            item_key="KEY_CHILD",
            parent_key="PAR_TOP",
            image_data=f"data:image/jpeg;base64,{b64_jpg}",
            paper_title="Parent Test Paper",
            page=4,
        )
    ]

    parts, text, notices = await _process_request_snippets(snips, "deepseek/deepseek-chat", None)
    assert parts == []
    assert len(notices) == 1
    assert "Image area attached, but active model (deepseek/deepseek-chat) is text-only. The agent has been notified." in notices[0]["notice"]
    assert 'key="PAR_TOP"' in text
    assert 'page="4"' in text
    assert "Parent Test Paper" in text
    assert "[Attached image area: untranscribed image on page 4." in text


@pytest.mark.asyncio
async def test_process_request_snippets_text_only_zero_delay_no_vlm_dependencies():
    """Verify text-only models process snippets immediately without any VLM dependencies or timeouts."""
    raw_jpg = b"\xff\xd8\xfffakejpegbytes"
    b64_jpg = base64.b64encode(raw_jpg).decode("ascii")
    snips = [
        SnippetItem(
            id="s_fast",
            type="image",
            item_key="KEY_FAST",
            image_data=f"data:image/jpeg;base64,{b64_jpg}",
            paper_title="Fast Paper",
            page=9,
        )
    ]

    start_time = time.monotonic()
    parts, text, notices = await _process_request_snippets(snips, "deepseek/deepseek-chat", None)
    elapsed = time.monotonic() - start_time

    assert elapsed < 0.1  # Zero delay, strictly synchronous path
    assert parts == []
    assert len(notices) == 1
    assert 'key="KEY_FAST"' in text
    assert "[Attached image area: untranscribed image on page 9." in text


def test_vlm_caption_with_openrouter_reasoning_suppression():
    """Verify _caption_with_openrouter passes reasoning suppression by default."""
    from zocrates.rag.vlm_captioner import VLMCaptioner

    captioner = VLMCaptioner(openrouter_api_key="sk-test-key")
    with patch("litellm.completion") as mock_completion, patch.object(
        captioner, "_encode_and_downscale_image", return_value=("image/png", "fakeb64")
    ):
        mock_resp = MagicMock()
        mock_resp.choices = [
            MagicMock(message=MagicMock(content="**Type**: Plot\n**Key Elements**: X, Y\n**Takeaway**: Trend."))
        ]
        mock_completion.return_value = mock_resp

        # 1. Default disable_reasoning=True passes extra_body
        caption, model_used = captioner._caption_with_openrouter(Path("/fake/image.png"))
        assert caption is not None
        assert mock_completion.call_count == 1
        call_kwargs = mock_completion.call_args[1]
        assert call_kwargs.get("extra_body") == {"reasoning": {"effort": "none"}}

        # 2. disable_reasoning=False omits extra_body
        mock_completion.reset_mock()
        caption, model_used = captioner._caption_with_openrouter(
            Path("/fake/image.png"), disable_reasoning=False
        )
        assert caption is not None
        assert mock_completion.call_count == 1
        call_kwargs_false = mock_completion.call_args[1]
        assert "extra_body" not in call_kwargs_false

        # 3. caption_image forwards disable_reasoning
        mock_completion.reset_mock()
        caption, model, source = captioner.caption_image(
            Path("/fake/image.png"), disable_reasoning=True
        )
        assert mock_completion.call_count == 1
        assert mock_completion.call_args[1].get("extra_body") == {"reasoning": {"effort": "none"}}

        mock_completion.reset_mock()
        caption, model, source = captioner.caption_image(
            Path("/fake/image.png"), disable_reasoning=False
        )
        assert mock_completion.call_count == 1
        assert "extra_body" not in mock_completion.call_args[1]


@pytest.mark.asyncio
async def test_all_branches_preserve_key_and_author_attributes():
    """Verify every branch of _process_request_snippets producing <image_snippet> preserves key and authors attributes."""
    raw_jpg = b"\xff\xd8\xfffakejpegbytes"
    b64_jpg = base64.b64encode(raw_jpg).decode("ascii")

    # Branch 1: Empty image data
    s_empty = SnippetItem(
        id="s1",
        type="image",
        item_key="KEY_1",
        parent_key="PAR_1",
        author_str="Author One",
        image_data=None,
        paper_title="Paper One",
    )
    _, text1, _ = await _process_request_snippets([s_empty], "google/gemini-2.0-flash-exp:free", None)
    assert '<image_snippet paper="Paper One" authors="Author One" page="?" key="PAR_1">' in text1

    # Branch 2: Decode error
    s_bad = SnippetItem(
        id="s2",
        type="image",
        item_key="KEY_2",
        author_str="Author Two",
        image_data="invalid-base64",
        paper_title="Paper Two",
    )
    _, text2, _ = await _process_request_snippets([s_bad], "google/gemini-2.0-flash-exp:free", None)
    assert '<image_snippet paper="Paper Two" authors="Author Two" page="?" key="KEY_2">' in text2

    # Branch 3: Vision model
    s_vision = SnippetItem(
        id="s3",
        type="image",
        parent_key="PAR_3",
        author_str="Author Three",
        image_data=f"data:image/jpeg;base64,{b64_jpg}",
        paper_title="Paper Three",
    )
    parts3, text3, _ = await _process_request_snippets([s_vision], "google/gemini-2.0-flash-exp:free", None)
    assert len(parts3) == 1
    assert '<image_snippet paper="Paper Three" authors="Author Three" page="?" key="PAR_3">' in text3

    # Branch 4: Text model with parent_key
    s_text_parent = SnippetItem(
        id="s4",
        type="image",
        item_key="KEY_4",
        parent_key="PAR_4",
        author_str="Author Four",
        image_data=f"data:image/jpeg;base64,{b64_jpg}",
        paper_title="Paper Four",
        page=3,
    )
    parts4, text4, notices4 = await _process_request_snippets([s_text_parent], "deepseek/deepseek-chat", None)
    assert parts4 == []
    assert '<image_snippet paper="Paper Four" authors="Author Four" page="3" key="PAR_4">' in text4
    assert "[Attached image area: untranscribed image on page 3. The active coordinator model (deepseek/deepseek-chat) does not support vision/image inputs. If needed, inspect the surrounding document text or section via retrieval tools.]" in text4
    assert len(notices4) == 1
    assert notices4[0]["notice"] == "Image area attached, but active model (deepseek/deepseek-chat) is text-only. The agent has been notified."

    # Branch 5: Text model with item_key fallback when parent_key is missing
    s_text_item = SnippetItem(
        id="s5",
        type="image",
        item_key="KEY_5",
        author_str="Author Five",
        image_data=f"data:image/jpeg;base64,{b64_jpg}",
        paper_title="Paper Five",
    )
    parts5, text5, notices5 = await _process_request_snippets([s_text_item], "deepseek/deepseek-chat", None)
    assert parts5 == []
    assert '<image_snippet paper="Paper Five" authors="Author Five" page="?" key="KEY_5">' in text5
    assert len(notices5) == 1
    assert notices5[0]["notice"] == "Image area attached, but active model (deepseek/deepseek-chat) is text-only. The agent has been notified."


def test_library_search_agent_instruction_mentions_focal_snippet_key():
    """Verify LIBRARY_SEARCH_AGENT_INSTRUCTION documents using the snippet key attribute."""
    assert "# Context: User-Selected Focal Snippets from Attachment" in LIBRARY_SEARCH_AGENT_INSTRUCTION
    assert "<text_snippet>" in LIBRARY_SEARCH_AGENT_INSTRUCTION
    assert "<image_snippet>" in LIBRARY_SEARCH_AGENT_INSTRUCTION
    assert 'key="..."' in LIBRARY_SEARCH_AGENT_INSTRUCTION
    assert "read_document(item_key=...)" in LIBRARY_SEARCH_AGENT_INSTRUCTION
    assert "get_document_toc(item_key=...)" in LIBRARY_SEARCH_AGENT_INSTRUCTION


