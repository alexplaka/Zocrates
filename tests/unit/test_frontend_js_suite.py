"""
Pytest wrapper executing the front-end JavaScript unit test suite and pre-flight syntax checks.
Orchestrates tests across all Zotero extension subsystems:
sidebar.js, scholarImport.js, autoConverter.js, batchConverter.js, infoModal.js, itemPane.js, bootstrap.js, graph.js.
"""

from pathlib import Path
import shutil
import subprocess
import pytest


def test_frontend_javascript_unit_suite():
    """Verify all front-end JS modules pass syntax checks and unit assertions."""
    node_bin = shutil.which("node")
    if not node_bin:
        pytest.skip("Node.js binary not available in environment.")

    root_dir = Path(__file__).resolve().parent.parent.parent

    # 1. Syntax checks across all add-on scripts
    js_files = [
        root_dir / "zotero-plugin/addon/lib/scholarImport.js",
        root_dir / "zotero-plugin/addon/lib/autoConverter.js",
        root_dir / "zotero-plugin/addon/lib/batchConverter.js",
        root_dir / "zotero-plugin/addon/lib/infoModal.js",
        root_dir / "zotero-plugin/addon/lib/itemPane.js",
        root_dir / "zotero-plugin/addon/lib/portOffset.js",
        root_dir / "zotero-plugin/addon/bootstrap.js",
        root_dir / "zotero-plugin/addon/sidebar.js",
        root_dir / "zotero-plugin/addon/preferences.js",
        root_dir / "zotero-plugin/addon/graph.js",
        root_dir / "zotero-plugin/addon/claims.js",
    ]

    for js_path in js_files:
        assert js_path.exists(), f"File {js_path} does not exist"
        proc = subprocess.run(
            [node_bin, "--check", str(js_path)],
            capture_output=True,
            text=True,
            cwd=str(root_dir),
        )
        assert proc.returncode == 0, f"Syntax check failed for {js_path}:\n{proc.stderr}"

    # 2. Run the dedicated unit test suite
    test_runner = root_dir / "tests/js/run_js_tests.js"
    if not test_runner.exists():
        test_runner = root_dir / "tests/run_js_tests.js"
    assert test_runner.exists(), "JS test runner missing"

    res = subprocess.run(
        [node_bin, str(test_runner)],
        capture_output=True,
        text=True,
        cwd=str(root_dir),
    )
    assert res.returncode == 0, f"JavaScript unit tests failed:\n{res.stdout}\n{res.stderr}"
    import re
    m = re.search(r"(\d+)/(\d+) tests passed", res.stdout)
    assert m, f"No test summary found in output:\n{res.stdout}"
    assert m.group(1) == m.group(2) and int(m.group(1)) >= 80, f"Some tests failed:\n{res.stdout}"
