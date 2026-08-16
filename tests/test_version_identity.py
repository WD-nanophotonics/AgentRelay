from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from dummy_orchestrator import __version__
from dummy_orchestrator import diagnostics, global_cli


ROOT = Path(__file__).resolve().parents[1]


def test_version_is_single_authoritative_source():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    version_source = (ROOT / "src" / "dummy_orchestrator" / "version.py").read_text(encoding="utf-8")
    assert 'dynamic = ["version"]' in pyproject
    assert "version = {attr = \"dummy_orchestrator.version.__version__\"}" in pyproject
    assert f'__version__ = "{__version__}"' in version_source
    assert diagnostics.__version__ == __version__
    assert global_cli.TOOL_VERSION == __version__


def test_cli_version_matches_authoritative_source():
    result = subprocess.run(
        [sys.executable, "-m", "dummy_orchestrator.global_cli", "--version"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == __version__


def test_source_identity_reports_checkout_identity():
    identity = global_cli.source_identity()
    assert identity["version"] == __version__
    assert identity["version_source"].endswith("dummy_orchestrator\\version.py")
    assert identity["install_source"] == str(ROOT)
    assert identity["package_path"].endswith("dummy_orchestrator\\global_cli.py")
    assert identity["git_revision"]
    assert identity["git_branch"]
    assert isinstance(identity["git_dirty"], bool)
    assert len(identity["source_fingerprint"]) == 16
    json.dumps(identity)
