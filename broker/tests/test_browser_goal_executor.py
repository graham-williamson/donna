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


def test_executor_self_bootstraps_broker_under_spawn_env(tmp_path):
    """Regression (deploy crash 2026-06-27): the broker spawns executors with a
    sanitised env (PATH only — NO PYTHONPATH) and an ephemeral cwd, so Python puts
    only the script's own dir (executors/) on sys.path. The executor imports
    `from broker import ...`, which is NOT resolvable from there — it must bootstrap
    the broker package's parent onto sys.path itself, or it dies at import with
    ModuleNotFoundError before reaching its own fail() handler (surfacing only as a
    bare `executor_crashed`).

    Reproduce the real spawn condition exactly: a fresh subprocess, PATH-only env,
    a temp cwd, valid stdin, and NO DONNA_CREDS_FD. The executor must reach its own
    error handling (graceful structured JSON — here `login_failed` for the missing
    creds fd), never a Python traceback.
    """
    import json
    import subprocess

    exe = Path(__file__).resolve().parents[1] / "executors" / "browser_goal"
    req = json.dumps({"capability": "browser_goal.plan",
                      "params": {"site": "everyone_active", "goal": "x", "phase": "plan"}})
    proc = subprocess.run(
        [sys.executable, str(exe)],
        input=req,
        env={"PATH": "/usr/bin:/bin"},   # mirror executor._sanitised_env: no PYTHONPATH
        cwd=str(tmp_path),               # ephemeral cwd, like the broker's mkdtemp
        capture_output=True,
        text=True,
    )
    assert "ModuleNotFoundError" not in proc.stderr, (
        "executor failed to bootstrap the broker package under the broker spawn "
        f"env:\n{proc.stderr}")
    # It reached its own fail() handler → structured JSON, not a crash.
    parsed = json.loads(proc.stdout)
    assert "error_code" in parsed


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
