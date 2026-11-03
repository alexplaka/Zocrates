"""Global pytest fixtures and network guards for hermetic testing.

This module ensures no outbound network calls are made to OpenRouter during test runs:
1. `mock_document_summary`: Autouse fixture that returns a deterministic fallback summary
   during document processing unless the test is part of `test_toc_generator.py` or
   explicitly mocks summary generation / litellm.
2. `block_openrouter_network_calls`: Session-scoped autouse fixture that intercepts any
   outbound calls targeting openrouter.ai via requests, httpx, or litellm.
"""

from unittest.mock import Mock

import httpx
import litellm
import pytest
import requests


@pytest.fixture(scope="session", autouse=True)
def block_openrouter_network_calls():
    """Intercept any outbound network calls to openrouter.ai across requests, httpx, and litellm."""
    orig_requests_send = requests.Session.send
    orig_httpx_send = httpx.Client.send
    orig_httpx_async_send = httpx.AsyncClient.send
    orig_litellm_completion = litellm.completion
    orig_litellm_acompletion = litellm.acompletion

    def _guarded_requests_send(self, request, **kwargs):
        url = str(getattr(request, "url", "") or "")
        if "openrouter.ai" in url:
            raise RuntimeError(f"Unmocked OpenRouter outbound call detected: {url}")
        return orig_requests_send(self, request, **kwargs)

    def _guarded_httpx_send(self, request, **kwargs):
        url = str(getattr(request, "url", "") or "")
        if "openrouter.ai" in url:
            raise RuntimeError(f"Unmocked OpenRouter outbound call detected: {url}")
        return orig_httpx_send(self, request, **kwargs)

    async def _guarded_httpx_async_send(self, request, **kwargs):
        url = str(getattr(request, "url", "") or "")
        if "openrouter.ai" in url:
            raise RuntimeError(f"Unmocked OpenRouter outbound call detected: {url}")
        return await orig_httpx_async_send(self, request, **kwargs)

    def _extract_openrouter_url(args, kwargs):
        model = ""
        if args:
            model = str(args[0])
        if "model" in kwargs:
            model = str(kwargs["model"])
        api_base = str(kwargs.get("api_base") or kwargs.get("base_url") or "")
        custom_provider = str(kwargs.get("custom_llm_provider") or "").lower()
        if "openrouter" in model.lower() or "openrouter.ai" in api_base.lower() or custom_provider == "openrouter":
            return api_base or "https://openrouter.ai"
        return None

    def _guarded_litellm_completion(*args, **kwargs):
        url = _extract_openrouter_url(args, kwargs)
        if url:
            raise RuntimeError(f"Unmocked OpenRouter outbound call detected: {url}")
        return orig_litellm_completion(*args, **kwargs)

    async def _guarded_litellm_acompletion(*args, **kwargs):
        url = _extract_openrouter_url(args, kwargs)
        if url:
            raise RuntimeError(f"Unmocked OpenRouter outbound call detected: {url}")
        return await orig_litellm_acompletion(*args, **kwargs)

    requests.Session.send = _guarded_requests_send
    httpx.Client.send = _guarded_httpx_send
    httpx.AsyncClient.send = _guarded_httpx_async_send
    litellm.completion = _guarded_litellm_completion
    litellm.acompletion = _guarded_litellm_acompletion

    try:
        yield
    finally:
        requests.Session.send = orig_requests_send
        httpx.Client.send = orig_httpx_send
        httpx.AsyncClient.send = orig_httpx_async_send
        litellm.completion = orig_litellm_completion
        litellm.acompletion = orig_litellm_acompletion


@pytest.fixture(autouse=True)
def mock_document_summary(request, monkeypatch):
    """Ensure document conversion does not attempt live LLM summary generation.

    Returns deterministic extractive summary fallback unless running in test_toc_generator.py
    or if the test has explicitly mocked summary generation or litellm.
    """
    node_id = getattr(request.node, "nodeid", "")
    fspath = str(getattr(request.node, "fspath", getattr(request, "path", "")))
    if "test_toc_generator.py" in fspath or "test_toc_generator" in node_id:
        yield
        return

    import zocrates.rag.document_processor as _dp
    import zocrates.rag.summary_generator as _sg

    orig_generate_summary = _sg.generate_summary

    def _safe_generate_summary(*args, **kwargs):
        sg_comp = getattr(_sg.litellm, "completion", None)
        root_comp = getattr(litellm, "completion", None)
        if isinstance(sg_comp, Mock) or isinstance(root_comp, Mock):
            return orig_generate_summary(*args, **kwargs)
        return ("Extractive summary of document.", "extractive")

    monkeypatch.setattr(_sg, "generate_summary", _safe_generate_summary)
    yield
