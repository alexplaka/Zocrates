"""Unit tests for Phase 3 deterministic backend port allocation.

Pins the cross-language Python/JavaScript parity formula: the backend port for
a library is derived as

    8765 + (first 8 bytes of the big-endian SHA-256 digest of the absolute
            zocrates-data directory path, mod 100)

The JavaScript mirror lives in ``zotero-plugin/addon/lib/portOffset.js`` and is
asserted against the same pinned integer in ``tests/js/test_port_parity.js``.
"""

import hashlib
import os
import socket
from pathlib import Path
from unittest.mock import patch

from zocrates.config import (
    BASE_BACKEND_PORT,
    DETERMINISTIC_PORT_SPAN,
    _parse_config_backend_port,
    _resolve_backend_port,
    compute_deterministic_port,
    find_free_port,
)

# Pinned cross-language parity vector: the deterministic port for the absolute
# Zotero data directory path "/home/alex/Zotero" (the zocrates-data subdir is
# appended by production callers, but the pure hash function accepts any path
# string). The identical integer is asserted in tests/js/test_port_parity.js.
PINNED_VECTOR_DIR = "/home/alex/Zotero"
PINNED_VECTOR_PORT = 8832


def _reference_offset(path: str) -> int:
    """Independent reference implementation of the pinned formula."""
    digest = hashlib.sha256(path.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % 100


class TestDeterministicPortOffset:
    """compute_deterministic_port: pinned formula, bounds, and determinism."""

    def test_pinned_parity_vector(self) -> None:
        """The exact pinned vector must hold for the Python implementation."""
        assert compute_deterministic_port(Path(PINNED_VECTOR_DIR)) == PINNED_VECTOR_PORT
        assert PINNED_VECTOR_PORT == 8765 + 67
        assert PINNED_VECTOR_PORT == BASE_BACKEND_PORT + _reference_offset(PINNED_VECTOR_DIR)

    def test_formula_matches_reference_implementation(self) -> None:
        """Formula must hold byte-for-byte for representative POSIX and Windows paths."""
        paths = [
            "/home/alex/Zotero",
            "/home/alex/Zotero/zocrates-data",
            "/home/alex/Zotero/Zotero test-user",
            "C:\\Users\\Researcher\\Zotero",
            "C:\\Users\\Researcher\\Zotero\\zocrates-data",
        ]
        for path in paths:
            expected = 8765 + _reference_offset(path)
            assert compute_deterministic_port(Path(path)) == expected, path

    def test_port_always_within_8765_8864(self) -> None:
        """Offset is mod 100, so the result is always inside 8765..8864."""
        for i in range(50):
            port = compute_deterministic_port(Path(f"/home/user/profile-{i}/zocrates-data"))
            assert BASE_BACKEND_PORT <= port < BASE_BACKEND_PORT + DETERMINISTIC_PORT_SPAN

    def test_same_path_is_deterministic(self) -> None:
        path = Path("/home/alex/Zotero/zocrates-data")
        assert compute_deterministic_port(path) == compute_deterministic_port(path)

    def test_utf8_multibyte_path(self) -> None:
        """Non-ASCII paths hash their UTF-8 bytes (matching TextEncoder in JS)."""
        path = Path("/home/ünïcode/Zötéro/zocrates-data")
        expected = 8765 + _reference_offset(str(path))
        assert compute_deterministic_port(path) == expected


def _reserve_free_port() -> tuple[socket.socket, int]:
    """Bind a listening TCP socket to an ephemeral port and return it."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    return sock, sock.getsockname()[1]


def _reserve_specific_port(port: int) -> socket.socket:
    """Bind a listening TCP socket to one specific port."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", port))
    sock.listen(1)
    return sock


class TestFindFreePort:
    """find_free_port: free-candidate selection, stepping, and saturation."""

    def test_returns_start_when_free(self) -> None:
        sock, port = _reserve_free_port()
        sock.close()
        # Port was just released: the next candidate probed from it is the
        # start port itself and must now be free.
        assert find_free_port(port, span=100) == port

    def test_steps_forward_when_occupied(self) -> None:
        sock, port = _reserve_free_port()
        try:
            assert find_free_port(port, span=100) == port + 1
        finally:
            sock.close()
        assert find_free_port(port, span=100) == port

    def test_all_candidates_busy_returns_start(self) -> None:
        sock_a, start = _reserve_free_port()
        sock_b = _reserve_specific_port(start + 1)
        try:
            assert find_free_port(start, span=2) == start
        finally:
            sock_a.close()
            sock_b.close()

    def test_default_span_is_100(self) -> None:
        assert find_free_port.__defaults__ == (100,)


class TestPortPrecedence:
    """_resolve_backend_port: env > config > deterministic offset > base."""

    def test_env_override_beats_config_and_offset(self) -> None:
        with patch.dict(os.environ, {"BACKEND_PORT": "9090"}), \
                patch("zocrates.config._parse_config_backend_port", return_value=8769), \
                patch("zocrates.config._resolve_import_time_zotero_data_dir", return_value=Path("/tmp/any")), \
                patch("zocrates.config.find_free_port") as probe:
            assert _resolve_backend_port() == 9090
            probe.assert_not_called()

    def test_config_override_beats_offset(self) -> None:
        with patch.dict(os.environ, {"BACKEND_PORT": ""}), \
                patch("zocrates.config._parse_config_backend_port", return_value=8769), \
                patch("zocrates.config._resolve_import_time_zotero_data_dir", return_value=Path("/tmp/any")), \
                patch("zocrates.config.find_free_port") as probe:
            assert _resolve_backend_port() == 8769
            probe.assert_not_called()

    def test_offset_tier_hashes_zocrates_data_subdir(self) -> None:
        data_dir = Path("/home/alex/Zotero-test")
        expected = compute_deterministic_port(data_dir / "zocrates-data")
        with patch.dict(os.environ, {"BACKEND_PORT": ""}), \
                patch("zocrates.config._parse_config_backend_port", return_value=None), \
                patch("zocrates.config._resolve_import_time_zotero_data_dir", return_value=data_dir), \
                patch("zocrates.config.find_free_port", side_effect=lambda start, span=100: start):
            assert _resolve_backend_port() == expected

    def test_no_data_dir_falls_back_to_base_without_probing(self) -> None:
        with patch.dict(os.environ, {"BACKEND_PORT": ""}), \
                patch("zocrates.config._parse_config_backend_port", return_value=None), \
                patch("zocrates.config._resolve_import_time_zotero_data_dir", return_value=None), \
                patch("zocrates.config.find_free_port", side_effect=AssertionError("must not probe")):
            assert _resolve_backend_port() == 8765

    def test_invalid_env_falls_through_to_lower_tiers(self) -> None:
        with patch.dict(os.environ, {"BACKEND_PORT": "not-a-port"}), \
                patch("zocrates.config._parse_config_backend_port", return_value=None), \
                patch("zocrates.config._resolve_import_time_zotero_data_dir", return_value=None):
            assert _resolve_backend_port() == 8765


class TestConfigBackendPortPeek:
    """_parse_config_backend_port: read-only, tolerant, int > 0 only."""

    def test_missing_config_returns_none(self, tmp_path: Path) -> None:
        assert _parse_config_backend_port(tmp_path) is None

    def test_explicit_int_port_returned(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "zocrates-data"
        config_dir.mkdir()
        (config_dir / "zocrates_config.json").write_text('{"backend_port": 8769}')
        assert _parse_config_backend_port(tmp_path) == 8769

    def test_null_port_returns_none(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "zocrates-data"
        config_dir.mkdir()
        (config_dir / "zocrates_config.json").write_text('{"backend_port": null}')
        assert _parse_config_backend_port(tmp_path) is None

    def test_non_positive_port_rejected(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "zocrates-data"
        config_dir.mkdir()
        (config_dir / "zocrates_config.json").write_text('{"backend_port": 0}')
        assert _parse_config_backend_port(tmp_path) is None

    def test_string_port_parsed(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "zocrates-data"
        config_dir.mkdir()
        (config_dir / "zocrates_config.json").write_text('{"backend_port": "8769"}')
        assert _parse_config_backend_port(tmp_path) == 8769

    def test_garbage_json_returns_none(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "zocrates-data"
        config_dir.mkdir()
        (config_dir / "zocrates_config.json").write_text("{not json")
        assert _parse_config_backend_port(tmp_path) is None

    def test_peek_performs_no_disk_writes(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "zocrates-data"
        config_dir.mkdir()
        (config_dir / "zocrates_config.json").write_text('{"backend_port": null}')
        before = sorted(p.name for p in config_dir.iterdir())
        assert _parse_config_backend_port(tmp_path) is None
        after = sorted(p.name for p in config_dir.iterdir())
        assert before == after
