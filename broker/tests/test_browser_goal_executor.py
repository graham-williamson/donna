from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
from pathlib import Path

# Ensure the broker package is importable (same as pytest pythonpath = [".."])
_REPO_ROOT = Path(__file__).resolve().parents[2]  # /donna
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Load the executor module directly by path (no .py extension — use SourceFileLoader).
_PATH = Path(__file__).resolve().parents[1] / "executors" / "browser_goal"
_loader = importlib.machinery.SourceFileLoader("browser_goal_exec", str(_PATH))
_spec = importlib.util.spec_from_loader("browser_goal_exec", _loader)
mod = importlib.util.module_from_spec(_spec)            # type: ignore[arg-type]
_spec.loader.exec_module(mod)                            # type: ignore[union-attr]


def test_accessibility_to_snapshot_shape():
    raw_nodes = [{"ref": "r1", "role": "button", "name": "Go", "tag": "button", "editable": False}]
    out = mod._accessibility_to_snapshot("https://x.everyoneactive.com", raw_nodes)
    assert out["url"] == "https://x.everyoneactive.com"
    assert out["nodes"][0]["ref"] == "r1" and out["nodes"][0]["role"] == "button"


def test_accessibility_to_snapshot_assigns_refs_when_missing():
    raw_nodes = [
        {"role": "link", "name": "Login", "tag": "a", "editable": False},
        {"role": "textbox", "name": "Email", "tag": "input", "editable": True},
    ]
    out = mod._accessibility_to_snapshot("https://example.com", raw_nodes)
    assert len(out["nodes"]) == 2
    # refs should be stable strings (enumerated when not provided)
    refs = [n["ref"] for n in out["nodes"]]
    assert all(isinstance(r, str) and r for r in refs)
    # second node should be editable
    assert out["nodes"][1]["editable"] is True


def test_fail_is_importable_without_playwright():
    # fail() must be importable; it should write JSON and call sys.exit(1)
    import io
    import json
    old_stdout = sys.stdout
    sys.stdout = io.StringIO()
    try:
        try:
            mod.fail("test_error", "some detail")
        except SystemExit as e:
            assert e.code == 1
        output = sys.stdout.getvalue()
    finally:
        sys.stdout = old_stdout
    parsed = json.loads(output)
    assert parsed["error_code"] == "test_error"
    assert parsed["detail"] == "some detail"
