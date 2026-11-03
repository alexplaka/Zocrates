"""Unit tests for the opt-in diagnostic service and multi-layer redaction engine."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from zocrates.utils.diagnostics import (
    ALLOWED_CONFIG_KEYS,
    MAX_LOG_LINE_CHARS,
    MAX_REPORT_BYTES,
    OMITTED_LOGS_TEXT,
    OMITTED_PROMPTS_TEXT,
    OMITTED_TITLE_TEXT,
    TRUNCATION_MARKER,
    build_diagnostic_report_bundle,
    build_diagnostic_snapshot,
    extract_sanitized_config,
    extract_shallow_turn_context,
    format_markdown_report,
    get_sanitized_log_tail,
    sanitize_text,
    sanitize_url,
)


class TestRedactionEngine:
    """Verify Layer 1 exact-value and Layer 2 pattern-based redaction rules."""

    def test_exact_secrets_and_paths_are_scrubbed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        zot_dir = tmp_path / "CustomZoteroProfile"
        raw = (
            f"Auth={ 'my-custom-auth-987654' } "
            f"OR={ 'custom_or_secret_value_abc' } "
            f"DL={ 'datalab_live_token_xyz999' } "
            f"OA={ 'openalex_live_token_445566' } "
            f"Dir={zot_dir}/storage/KEY1/paper.pdf "
            f"Home={Path.home()}/Documents/notes.md"
        )
        monkeypatch.setenv("ZOCRATES_AUTH_TOKEN", "my-custom-auth-987654")
        with (
            patch("zocrates.utils.diagnostics.get_secure_api_key", return_value="custom_or_secret_value_abc"),
            patch("zocrates.utils.diagnostics.get_datalab_api_key", return_value="datalab_live_token_xyz999"),
            patch("zocrates.utils.diagnostics.get_openalex_api_key", return_value="openalex_live_token_445566"),
        ):
            cleaned = sanitize_text(raw, zotero_data_dir=zot_dir)

        assert "my-custom-auth-987654" not in cleaned
        assert "custom_or_secret_value_abc" not in cleaned
        assert "datalab_live_token_xyz999" not in cleaned
        assert "openalex_live_token_445566" not in cleaned
        assert str(zot_dir) not in cleaned
        assert str(Path.home()) not in cleaned
        assert "[REDACTED_AUTH_TOKEN]" in cleaned
        assert "[REDACTED_API_KEY]" in cleaned
        assert "<ZOTERO_DATA_DIR>/storage/KEY1/paper.pdf" in cleaned
        assert "<HOME>/Documents/notes.md" in cleaned

    def test_regex_scrubs_api_keys_bearer_headers_and_query_tokens(self) -> None:
        sample = (
            "OpenRouter key sk-or-v1-a1b2c3d4e5f60718293a4b5c6d7e8f90 "
            "OpenAI key sk-proj-AbCdEfGhIjKlMnOpQrStUvWxYz123456 "
            "Header Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.secret "
            "Custom X-Zocrates-Auth: deadbeef0123456789abcdef "
            "URL http://127.0.0.1:8765/chat?token=abcdef0123456789&foo=bar"
        )
        cleaned = sanitize_text(sample)

        assert "sk-or-v1-" not in cleaned
        assert "sk-proj-" not in cleaned
        assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in cleaned
        assert "deadbeef0123456789abcdef" not in cleaned
        assert "abcdef0123456789" not in cleaned
        assert "[REDACTED_OPENROUTER_KEY]" in cleaned
        assert "[REDACTED_API_KEY]" in cleaned
        assert "Bearer [REDACTED_TOKEN]" in cleaned
        assert "X-Zocrates-Auth: [REDACTED_AUTH_TOKEN]" in cleaned
        assert "token=[REDACTED_TOKEN]&foo=bar" in cleaned

    def test_regex_scrubs_emails_and_mailto_links(self) -> None:
        sample = (
            "User-Agent: Zocrates/0.1.0 (mailto:dr.jane_doe+lab@stanford.edu) "
            "Contact: alex.researcher@ox.ac.uk for access."
        )
        cleaned = sanitize_text(sample)

        assert "dr.jane_doe" not in cleaned
        assert "stanford.edu" not in cleaned
        assert "alex.researcher@ox.ac.uk" not in cleaned
        assert "mailto:[REDACTED_EMAIL]" in cleaned
        assert "[REDACTED_EMAIL]" in cleaned

    def test_regex_scrubs_multi_os_paths_and_preserves_https_urls(self) -> None:
        sample = "\n".join(
            [
                "Linux: /home/alice/Zotero/zocrates-data/logs/zocrates.log",
                "Silverblue: /var/home/bob/.var/app/org.zotero.Zotero/data",
                "SQLite URI: sqlite+aiosqlite:////home/charlie/Zotero/sessions.db",
                "macOS: /Users/diana/Zotero/storage/ABCD1234/paper.pdf",
                r"Windows backslash: C:\Users\EvanUser\Zotero\zotero.sqlite",
                "Windows slash: D:/Users/FionaUser/Zotero/zotero.sqlite",
                r'Windows JSON: {"path": "C:\\Users\\GeorgeUser\\AppData\\Roaming\\Zotero"}',
                r"Windows UNC: \\nas-server\research_lab\papers\smith2025.pdf",
                r'Windows JSON UNC: "\\\\corp-fs01\\private$\\zotero"',
                "GitHub URL: https://github.com/alexplaka/Zocrates/issues/new",
            ]
        )
        cleaned = sanitize_text(sample)

        for username in (
            "alice",
            "bob",
            "charlie",
            "diana",
            "EvanUser",
            "FionaUser",
            "GeorgeUser",
            "nas-server",
            "corp-fs01",
        ):
            assert username not in cleaned, f"Leaked identifier: {username}"

        assert "<HOME>/Zotero/zocrates-data/logs/zocrates.log" in cleaned
        assert "<HOME>/.var/app/org.zotero.Zotero/data" in cleaned
        assert "<HOME>/Zotero/storage/ABCD1234/paper.pdf" in cleaned
        assert r"<HOME>\Zotero\zotero.sqlite" in cleaned
        assert "<HOME>/Zotero/zotero.sqlite" in cleaned
        assert r"<HOME>\\AppData\\Roaming\\Zotero" in cleaned
        assert "<UNC_PATH>" in cleaned
        # Public HTTPS URLs must not be mangled by UNC rules
        assert "https://github.com/alexplaka/Zocrates/issues/new" in cleaned


class TestConfigAllowlistAndReadOnlyInvariants:
    """Verify strict configuration allowlisting, URL credential stripping, and zero disk writes."""

    def test_config_allowlist_omits_personal_data_and_strips_url_credentials(
        self, tmp_path: Path
    ) -> None:
        zocrates_dir = tmp_path / "zocrates-data"
        zocrates_dir.mkdir(parents=True)
        cfg_file = zocrates_dir / "zocrates_config.json"
        cfg_file.write_text(
            json.dumps(
                {
                    "llm_provider": "openai_compatible",
                    "llm_model": "qwen2.5-72b",
                    "llm_base_url": "http://secretuser:secretpass@192.168.1.50:8000/v1?api_key=xyz987#frag",
                    "scholar_polite_email": "private.scholar@mit.edu",
                    "user_research_profile": "PhD student working on confidential DARPA grant XYZ",
                    "unexpected_secret_field": "top-secret-internal-value",
                    "enable_mcp_server": True,
                    "enable_adaptive_compaction": False,
                    "max_saved_sessions": 75,
                }
            ),
            encoding="utf-8",
        )

        sanitized_cfg = extract_sanitized_config(tmp_path)

        assert "scholar_polite_email" not in sanitized_cfg
        assert "user_research_profile" not in sanitized_cfg
        assert "unexpected_secret_field" not in sanitized_cfg
        assert "private.scholar@mit.edu" not in json.dumps(sanitized_cfg)
        assert "DARPA" not in json.dumps(sanitized_cfg)

        # Allowed keys + sanitized llm_base_url
        extra_keys = set(sanitized_cfg.keys()) - set(ALLOWED_CONFIG_KEYS)
        assert extra_keys <= {"llm_base_url"}
        assert sanitized_cfg["llm_base_url"] == "http://192.168.1.50:8000/v1"
        assert sanitized_cfg["enable_mcp_server"] is True
        assert sanitized_cfg["adaptive_compaction_enabled"] is False
        assert sanitized_cfg["session_max_count"] == 75

    def test_sanitize_url_strips_userinfo_query_and_fragment(self) -> None:
        url = "https://admin:hunter2@llm.internal.org:8443/v1/chat?token=12345#section"
        assert sanitize_url(url) == "https://llm.internal.org:8443/v1/chat"

    def test_uninitialized_directory_performs_zero_disk_writes(self, tmp_path: Path) -> None:
        empty_zot_dir = tmp_path / "uninitialized_profile"
        assert not empty_zot_dir.exists()

        bundle = build_diagnostic_report_bundle(
            zotero_data_dir=empty_zot_dir,
            error_message="401 Unauthorized Missing Authentication Header",
            include_logs=True,
        )
        assert bundle["status"] == "success"
        assert "401 Unauthorized" in bundle["markdown"]
        # Must NOT have created zocrates-data, backend.auth, logs/, or zocrates_config.json
        assert not empty_zot_dir.exists()

    def test_sidecar_auth_read_does_not_mutate_cached_auth_token(self, tmp_path: Path) -> None:
        import zocrates.config as config_mod

        sidecar_dir = tmp_path / "zocrates-data"
        sidecar_dir.mkdir(parents=True)
        (sidecar_dir / "backend.auth").write_text("disk-sidecar-token-xyz\n", encoding="utf-8")

        orig_cached = config_mod._CACHED_AUTH_TOKEN
        try:
            config_mod._CACHED_AUTH_TOKEN = "in-memory-session-token-abc"
            cleaned = sanitize_text(
                "Tokens: disk-sidecar-token-xyz and in-memory-session-token-abc",
                zotero_data_dir=tmp_path,
            )
            assert "disk-sidecar-token-xyz" not in cleaned
            assert "in-memory-session-token-abc" not in cleaned
            assert config_mod._CACHED_AUTH_TOKEN == "in-memory-session-token-abc"
        finally:
            config_mod._CACHED_AUTH_TOKEN = orig_cached


class TestLogTailReader:
    """Verify binary EOF seek, rotation fallback, line caps, and fail-open behavior."""

    def test_missing_log_directory_fails_open(self, tmp_path: Path) -> None:
        lines = get_sanitized_log_tail(tmp_path / "nonexistent", max_lines=100)
        assert lines == []

    def test_rotation_fallback_combines_log_1_and_current_log(self, tmp_path: Path) -> None:
        log_dir = tmp_path / "zocrates-data" / "logs"
        log_dir.mkdir(parents=True)

        rotated = log_dir / "zocrates.log.1"
        current = log_dir / "zocrates.log"

        rotated_lines = [
            f"2026-04-01T10:00:{i:02d}  INFO      old_line_{i} /home/alice/doc.pdf"
            for i in range(45)
        ]
        current_lines = [
            f"2026-04-01T10:01:{i:02d}  ERROR     new_line_{i} Bearer secret_tok_{i}"
            for i in range(15)
        ]
        rotated.write_text("\n".join(rotated_lines) + "\n", encoding="utf-8")
        current.write_text("\n".join(current_lines) + "\n", encoding="utf-8")

        tail = get_sanitized_log_tail(tmp_path, max_lines=50)
        assert len(tail) == 50
        # First 35 of tail come from the end of zocrates.log.1 (indices 10..44)
        assert "old_line_10" in tail[0]
        assert "alice" not in tail[0]
        assert "<HOME>/doc.pdf" in tail[0]
        # Last 15 come from zocrates.log
        assert "new_line_14" in tail[-1]
        assert "secret_tok_14" not in tail[-1]
        assert "Bearer [REDACTED_TOKEN]" in tail[-1]

    def test_binary_seek_handles_large_files_invalid_utf8_and_500_char_cap(
        self, tmp_path: Path
    ) -> None:
        log_dir = tmp_path / "zocrates-data" / "logs"
        log_dir.mkdir(parents=True)
        current = log_dir / "zocrates.log"

        # Build >300 KB file with invalid UTF-8 bytes near the start and a >500 char line with a key near char 490
        filler = b"2026-04-01 INFO filler log entry padding line\n" * 7000
        long_line = (
            "2026-04-01 ERROR "
            + ("x" * 475)
            + " sk-or-v1-99887766554433221100aabbccddeeff "
            + ("y" * 100)
            + "\n"
        ).encode("utf-8")
        current.write_bytes(b"\xff\xfe\xfd" + filler + long_line)

        tail = get_sanitized_log_tail(tmp_path, max_lines=60)
        assert len(tail) == 60
        last = tail[-1]
        assert len(last) <= MAX_LOG_LINE_CHARS
        assert "sk-or-v1-" not in last
        assert "9988776655443322" not in last


class TestPrivacyTogglesAndShallowTrace:
    """Verify opt-in privacy gates and strict exclusion of chunk payloads and thoughts."""

    def _build_mock_session(self) -> SimpleNamespace:
        user_part = SimpleNamespace(
            thought=None,
            function_call=None,
            function_response=None,
            text="Summarize proprietary unpublished manuscript by alice@harvard.edu",
        )
        user_evt = SimpleNamespace(
            id="evt_u1",
            author="user",
            invocation_id="inv_123",
            content=SimpleNamespace(parts=[user_part]),
            actions=SimpleNamespace(
                state_delta={
                    "active_context_items": [
                        {"key": "A7XYZ890", "title": "Confidential Pre-Print On Fusion"},
                    ]
                },
                compaction=None,
                rewind_before_invocation_id=None,
            ),
        )

        # Coordinator event with internal thought + tool call containing sensitive query value
        thought_part = SimpleNamespace(
            thought="Secret internal chain-of-thought reasoning that must never be exported",
            function_call=None,
            function_response=None,
            text=None,
        )
        fc_part = SimpleNamespace(
            thought=None,
            function_call=SimpleNamespace(
                name="search_library",
                args={"query": "sensitive unpublished fusion query", "scope_hint": "active"},
                id="fc_1",
            ),
            function_response=None,
            text=None,
        )
        coord_evt = SimpleNamespace(
            id="evt_a1",
            author="coordinator_agent",
            invocation_id="inv_123",
            isolation_scope=None,
            content=SimpleNamespace(parts=[thought_part, fc_part]),
            actions=None,
        )

        # Isolation-scope subagent retrieval event containing copyrighted paper chunk text
        iso_fr_part = SimpleNamespace(
            thought=None,
            function_call=None,
            function_response=SimpleNamespace(
                name="search_corpus",
                id="fc_sub_1",
                response={
                    "results": [
                        {"text": "COPYRIGHTED_VERBATIM_PAPER_CHUNK_TEXT_DO_NOT_LEAK"}
                    ]
                },
            ),
            text="SUBAGENT_ISOLATION_TEXT_DO_NOT_LEAK",
        )
        iso_evt = SimpleNamespace(
            id="evt_iso_1",
            author="library_search_agent",
            invocation_id="inv_123",
            isolation_scope="search_library_pipeline:fc_1",
            content=SimpleNamespace(parts=[iso_fr_part]),
            actions=None,
        )

        # Tool response on coordinator scope with an error + raw chunk field
        fr_part = SimpleNamespace(
            thought=None,
            function_call=None,
            function_response=SimpleNamespace(
                name="search_library",
                id="fc_1",
                response={
                    "status": "error",
                    "error": "Upstream 401 for Bearer live_tok_999 at /Users/bob/Zotero",
                    "evidence_chunks": "COPYRIGHTED_CHUNK_PAYLOAD_2_DO_NOT_LEAK",
                },
            ),
            text=None,
        )
        asst_text_part = SimpleNamespace(
            thought=None,
            function_call=None,
            function_response=None,
            text="Assistant error explanation mentioning /home/alice/paper.pdf",
        )
        final_evt = SimpleNamespace(
            id="evt_a2",
            author="coordinator_agent",
            invocation_id="inv_123",
            isolation_scope=None,
            content=SimpleNamespace(parts=[fr_part, asst_text_part]),
            actions=None,
        )

        return SimpleNamespace(
            id="sess_abc",
            state={},
            events=[user_evt, coord_evt, iso_evt, final_evt],
        )

    def test_defaults_omit_prompts_titles_and_logs(self, tmp_path: Path) -> None:
        log_dir = tmp_path / "zocrates-data" / "logs"
        log_dir.mkdir(parents=True)
        (log_dir / "zocrates.log").write_text("SECRET_LOG_LINE_XYZ\n", encoding="utf-8")

        sess = self._build_mock_session()
        snapshot = build_diagnostic_snapshot(
            zotero_data_dir=tmp_path,
            session=sess,
            invocation_id="inv_123",
            error_message="401 Unauthorized",
            # All 3 toggles left at default (False)
        )
        md = format_markdown_report(snapshot)

        assert OMITTED_PROMPTS_TEXT in md
        assert OMITTED_LOGS_TEXT in md
        assert f"[Key: A7XYZ890] {OMITTED_TITLE_TEXT}" in md

        # Strictly omitted content
        assert "unpublished manuscript" not in md
        assert "Confidential Pre-Print On Fusion" not in md
        assert "SECRET_LOG_LINE_XYZ" not in md
        assert "Assistant error explanation" not in md

        # Shallow trace still records tool name, arg_keys ONLY, and sanitized error
        assert "search_library" in md
        assert "query, scope_hint" in md
        assert "sensitive unpublished fusion query" not in md
        assert "COPYRIGHTED_VERBATIM_PAPER_CHUNK_TEXT_DO_NOT_LEAK" not in md
        assert "COPYRIGHTED_CHUNK_PAYLOAD_2_DO_NOT_LEAK" not in md
        assert "SUBAGENT_ISOLATION_TEXT_DO_NOT_LEAK" not in md
        assert "chain-of-thought" not in md
        assert "live_tok_999" not in md
        assert "bob" not in md

    def test_frontend_notifications_paper_and_note_titles_scrubbed_when_toggle_off(
        self, tmp_path: Path
    ) -> None:
        """Verify Converting, Re-converting, and double-quoted note/paper titles in notifications are scrubbed."""
        frontend_meta = {
            "notifications": [
                {
                    "type": "info",
                    "text": "⚡ Re-converting: Deep Quantum Systems with Docling Local...",
                    "timestamp": "2026-04-01T12:00:00Z",
                },
                {
                    "type": "success",
                    "text": 'Saved "My Quantum Research Summary" attached directly under "Attention Paper"',
                    "timestamp": "2026-04-01T12:01:00Z",
                },
                {
                    "type": "error",
                    "text": "Indexing failed: Secret Fusion Manuscript (SQLite busy)",
                    "timestamp": "2026-04-01T12:02:00Z",
                },
            ]
        }

        # With include_item_titles=False
        snapshot_off = build_diagnostic_snapshot(
            zotero_data_dir=tmp_path,
            include_item_titles=False,
            frontend_metadata=frontend_meta,
        )
        md_off = format_markdown_report(snapshot_off)

        assert "Deep Quantum Systems" not in md_off
        assert "My Quantum Research Summary" not in md_off
        assert "Attention Paper" not in md_off
        assert "Secret Fusion Manuscript" not in md_off
        assert f"⚡ Re-converting: {OMITTED_TITLE_TEXT} with" in md_off
        assert f'Saved "{OMITTED_TITLE_TEXT}" attached directly under "{OMITTED_TITLE_TEXT}"' in md_off
        assert f"Indexing failed: {OMITTED_TITLE_TEXT} (" in md_off

        # With include_item_titles=True
        snapshot_on = build_diagnostic_snapshot(
            zotero_data_dir=tmp_path,
            include_item_titles=True,
            frontend_metadata=frontend_meta,
        )
        md_on = format_markdown_report(snapshot_on)
        assert "Deep Quantum Systems" in md_on
        assert "My Quantum Research Summary" in md_on
        assert "Attention Paper" in md_on

    def test_toggles_enabled_include_sanitized_prompts_titles_and_logs_without_chunks(
        self, tmp_path: Path
    ) -> None:
        log_dir = tmp_path / "zocrates-data" / "logs"
        log_dir.mkdir(parents=True)
        (log_dir / "zocrates.log").write_text(
            "2026-04-01 ERROR OpenRouter rejected key sk-or-v1-11223344556677889900\n",
            encoding="utf-8",
        )

        sess = self._build_mock_session()
        snapshot = build_diagnostic_snapshot(
            zotero_data_dir=tmp_path,
            session=sess,
            invocation_id="inv_123",
            include_prompts=True,
            include_item_titles=True,
            include_logs=True,
        )
        md = format_markdown_report(snapshot)

        # Included & sanitized
        assert "Summarize proprietary unpublished manuscript by [REDACTED_EMAIL]" in md
        assert "[Key: A7XYZ890] Confidential Pre-Print On Fusion" in md
        assert "Assistant error explanation mentioning <HOME>/paper.pdf" in md
        assert "OpenRouter rejected key [REDACTED_OPENROUTER_KEY]" in md

        # Raw chunks, tool arg values, and thoughts STILL excluded even with all toggles ON
        assert "COPYRIGHTED_VERBATIM_PAPER_CHUNK_TEXT_DO_NOT_LEAK" not in md
        assert "COPYRIGHTED_CHUNK_PAYLOAD_2_DO_NOT_LEAK" not in md
        assert "sensitive unpublished fusion query" not in md
        assert "chain-of-thought" not in md


class TestReportSizeCeiling:
    """Verify collapsible GitHub Markdown formatting and hard 50 KB size ceiling."""

    def test_format_markdown_enforces_50kb_ceiling_with_truncation_marker(self) -> None:
        huge_logs = [f"2026-04-01 ERROR trace line {i}: " + ("A" * 450) for i in range(500)]
        huge_trace = [
            {
                "tool": f"tool_{i}",
                "author": "Zocrates",
                "arg_keys": ["query", "item_key", "limit"],
                "status": "error",
                "error": "E" * 250,
            }
            for i in range(150)
        ]
        snapshot = {
            "generated_at": "2026-04-01T12:00:00Z",
            "environment": {
                "os_platform": "Linux",
                "os_release": "6.8",
                "architecture": "x86_64",
                "python_version": "3.13.2",
                "backend_version": "0.1.0",
                "backend_port": 8765,
                "backend_pid": 4242,
                "hardware": {"ram_gb": 32.0, "gpu_vendor": "nvidia", "gpu_name": "RTX 4090"},
                "docling_assessment": {"recommended_mode": "full", "reason": "GPU ready"},
            },
            "frontend": {
                "zotero_version": "10.0.1",
                "zotero_platform": "Linux x86_64",
                "extension_version": "0.1.0",
                "window_mode": "sidebar",
                "backend_connection": "Connected",
                "heartbeat_latency_ms": 4.2,
                "notifications": [],
            },
            "config": {"llm_provider": "openrouter_llm"},
            "api_key_status": {"has_openrouter_key": True},
            "turn_context": {
                "session_id": "sess_huge",
                "invocation_id": "inv_huge",
                "error_message": "Error " + ("X" * 20000),
                "context_chips": [],
                "prompts": {
                    "included": True,
                    "user_prompt": "Prompt " + ("P" * 30000),
                    "assistant_response": "Resp " + ("R" * 30000),
                },
                "execution_trace": huge_trace,
            },
            "logs": {
                "included": True,
                "lines": huge_logs,
            },
        }

        md = format_markdown_report(snapshot)
        encoded_len = len(md.encode("utf-8"))

        assert encoded_len <= MAX_REPORT_BYTES
        assert TRUNCATION_MARKER in md
        assert "<details>" in md
        assert "</details>" in md
