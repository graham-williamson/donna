"""Tests for the promoter_client executor's PURE wire helpers (Plan B Task 6).

The actual ``socket.connect`` round-trip (``_round_trip``) is ``# pragma: no
cover`` — Graham smoke-tests it against the live daemon. The framing helpers
``encode_request`` / ``decode_reply`` share the daemon's wire format and carry
the real coverage here, including adversarial replies. We also confirm the
encoded request is exactly what ``promoter_daemon.handle_frame`` accepts, so
the two ends provably agree on the format.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import json
import struct
import sys
from pathlib import Path
from typing import Any

import pytest

# Ensure the broker package is importable (same as pytest pythonpath = [".."]).
_REPO_ROOT = Path(__file__).resolve().parents[2]  # /donna
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Load the executor module directly by path (no .py extension).
_PATH = Path(__file__).resolve().parents[1] / "executors" / "promoter_client"
_loader = importlib.machinery.SourceFileLoader("promoter_client_exec", str(_PATH))
_spec = importlib.util.spec_from_loader("promoter_client_exec", _loader)
mod = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(mod)  # type: ignore[union-attr]


# ---- encode_request --------------------------------------------------------


def test_encode_request_is_length_prefixed_install_frame() -> None:
    frame = mod.encode_request("waitrose")
    (declared,) = struct.unpack(">I", frame[:4])
    body = frame[4:]
    assert len(body) == declared
    obj = json.loads(body)
    assert obj == {"op": "install_pack", "pack_id": "waitrose"}


def test_encode_request_round_trips_through_daemon_handle_frame() -> None:
    """The request the client encodes is exactly what the daemon accepts —
    the two ends agree on the wire format."""
    from broker import promoter_daemon as d

    seen: dict[str, str] = {}

    def do_install(*, pack_id: str) -> dict[str, str]:
        seen["pack_id"] = pack_id
        return {"outcome": "installed", "pack_id": pack_id, "key_id": "k1"}

    reply = d.handle_frame(mod.encode_request("waitrose"), do_install=do_install)
    assert seen["pack_id"] == "waitrose"
    # And the client can decode the daemon's reply.
    decoded = mod.decode_reply(reply)
    assert decoded["ok"] is True
    assert decoded["pack_id"] == "waitrose"
    assert decoded["outcome"] == "installed"


# ---- decode_reply ----------------------------------------------------------


def _reply_frame(obj: Any) -> bytes:
    body = json.dumps(obj).encode("utf-8")
    return struct.pack(">I", len(body)) + body


def test_decode_reply_ok() -> None:
    out = mod.decode_reply(_reply_frame({"ok": True, "pack_id": "x"}))
    assert out == {"ok": True, "pack_id": "x"}


def test_decode_reply_short_prefix_raises() -> None:
    with pytest.raises(ValueError):
        mod.decode_reply(b"\x00\x01")


def test_decode_reply_body_shorter_than_declared_raises() -> None:
    raw = struct.pack(">I", 100) + b"{}"
    with pytest.raises(ValueError):
        mod.decode_reply(raw)


def test_decode_reply_oversized_declared_raises() -> None:
    raw = struct.pack(">I", 10_000_000) + b"{}"
    with pytest.raises(ValueError):
        mod.decode_reply(raw)


def test_decode_reply_non_object_raises() -> None:
    with pytest.raises(ValueError):
        mod.decode_reply(_reply_frame([1, 2, 3]))


# ---- fail ------------------------------------------------------------------


def test_fail_writes_structured_error_and_exits() -> None:
    old_stdout = sys.stdout
    sys.stdout = io.StringIO()
    try:
        with pytest.raises(SystemExit) as e:
            mod.fail("promoter_unreachable", "socket gone")
        assert e.value.code == 1
        output = sys.stdout.getvalue()
    finally:
        sys.stdout = old_stdout
    parsed = json.loads(output)
    assert parsed["error_code"] == "promoter_unreachable"
    assert parsed["detail"] == "socket gone"
