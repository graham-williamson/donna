"""Tests for promoter_daemon — the pure framing/dispatch + peer-cred core.

The accept loop, real socket, launchctl, and main() wiring are deploy-time and
Graham-smoke-tested; they carry ``# pragma: no cover`` justifications in the
module. The security surface — ``handle_frame`` and ``peer_uid_allowed`` — is
fully exercised here, including adversarial inputs.
"""
from __future__ import annotations

import json
import struct
from typing import Any

from broker import promoter_daemon as d


def _frame(obj: Any) -> bytes:
    body = json.dumps(obj).encode()
    return struct.pack(">I", len(body)) + body


def _decode(reply: bytes) -> dict[str, Any]:
    (length,) = struct.unpack(">I", reply[:4])
    body = reply[4:]
    assert len(body) == length
    result: dict[str, Any] = json.loads(body)
    return result


# --- handle_frame: happy path ------------------------------------------------


def test_handle_install_ok() -> None:
    def do_install(*, pack_id: str) -> dict[str, str]:
        assert pack_id == "waitrose"
        return {"outcome": "installed", "pack_id": "waitrose", "key_id": "k1"}

    reply = d.handle_frame(
        _frame({"op": "install_pack", "pack_id": "waitrose"}),
        do_install=do_install,
    )
    body = _decode(reply)
    assert body["ok"] is True
    assert body["pack_id"] == "waitrose"
    assert body["outcome"] == "installed"
    assert body["key_id"] == "k1"


# --- handle_frame: adversarial -----------------------------------------------


def test_handle_unknown_op_errors() -> None:
    def do_install(*, pack_id: str) -> dict[str, str]:
        raise AssertionError("do_install must NOT run for an unknown op")

    reply = d.handle_frame(_frame({"op": "rm_rf", "path": "/"}), do_install=do_install)
    body = _decode(reply)
    assert body["ok"] is False
    assert "op" in body["error"].lower()


def test_handle_missing_op_errors() -> None:
    def do_install(*, pack_id: str) -> dict[str, str]:
        raise AssertionError("do_install must NOT run when op is absent")

    reply = d.handle_frame(
        _frame({"pack_dir": "/p", "request_id": "R"}), do_install=do_install
    )
    assert _decode(reply)["ok"] is False


def test_handle_bad_json_body_errors() -> None:
    raw = struct.pack(">I", 3) + b"{!}"

    def do_install(*, pack_id: str) -> dict[str, str]:
        raise AssertionError("do_install must NOT run for malformed JSON")

    reply = d.handle_frame(raw, do_install=do_install)
    assert _decode(reply)["ok"] is False


def test_handle_non_object_json_errors() -> None:
    # A valid JSON value that is not an object (e.g. a list) must be rejected.
    reply = d.handle_frame(_frame([1, 2, 3]), do_install=lambda **k: {})
    assert _decode(reply)["ok"] is False


def test_handle_truncated_length_prefix_errors() -> None:
    # Fewer than 4 bytes: cannot even read the declared length.
    reply = d.handle_frame(b"\x00\x01", do_install=lambda **k: {})
    assert _decode(reply)["ok"] is False


def test_handle_body_shorter_than_declared_errors() -> None:
    # Declared length says 100 bytes but only a few are present.
    raw = struct.pack(">I", 100) + b"{}"
    reply = d.handle_frame(raw, do_install=lambda **k: {})
    assert _decode(reply)["ok"] is False


def test_handle_oversized_declared_length_errors_no_alloc() -> None:
    # Declared length far exceeds max_frame; reject WITHOUT trusting/reading it.
    raw = struct.pack(">I", 10_000_000) + b"{}"

    def do_install(*, pack_id: str) -> dict[str, str]:
        raise AssertionError("do_install must NOT run for an oversized frame")

    reply = d.handle_frame(raw, do_install=do_install, max_frame=1024)
    body = _decode(reply)
    assert body["ok"] is False
    assert "frame" in body["error"].lower() or "large" in body["error"].lower()


def test_handle_missing_pack_id_errors() -> None:
    def do_install(*, pack_id: str) -> dict[str, str]:
        raise AssertionError("do_install must NOT run when pack_id is missing")

    reply = d.handle_frame(
        _frame({"op": "install_pack"}), do_install=do_install
    )
    body = _decode(reply)
    assert body["ok"] is False
    assert "pack_id" in body["error"]


def test_handle_non_string_pack_id_errors() -> None:
    def do_install(*, pack_id: str) -> dict[str, str]:
        raise AssertionError("do_install must NOT run for wrong field types")

    reply = d.handle_frame(
        _frame({"op": "install_pack", "pack_id": 5}),
        do_install=do_install,
    )
    assert _decode(reply)["ok"] is False


def test_handle_traversal_pack_id_rejected_without_do_install() -> None:
    """A pack_id with a path separator or '..' must be rejected at the
    validation layer — do_install is NEVER called for it."""
    def do_install(*, pack_id: str) -> dict[str, str]:
        raise AssertionError("do_install must NOT run for an unsafe pack_id")

    for bad in ("../etc", "a/b", "..", "/abs", ".hidden"):
        reply = d.handle_frame(
            _frame({"op": "install_pack", "pack_id": bad}),
            do_install=do_install,
        )
        body = _decode(reply)
        assert body["ok"] is False
        assert "pack_id" in body["error"]


def test_handle_install_raises_becomes_error_reply() -> None:
    def boom(*, pack_id: str) -> dict[str, str]:
        raise RuntimeError("refused: bad sig")

    reply = d.handle_frame(
        _frame({"op": "install_pack", "pack_id": "waitrose"}),
        do_install=boom,
    )
    body = _decode(reply)
    assert body["ok"] is False
    assert "refused: bad sig" in body["error"]


def test_handle_promoter_error_reason_relayed() -> None:
    from broker.promoter import PromoterError

    def refuse(*, pack_id: str) -> dict[str, str]:
        raise PromoterError("refused: no matching install approval")

    reply = d.handle_frame(
        _frame({"op": "install_pack", "pack_id": "waitrose"}),
        do_install=refuse,
    )
    body = _decode(reply)
    assert body["ok"] is False
    assert "no matching install approval" in body["error"]


# --- safe_pack_id (pure) -----------------------------------------------------


def test_safe_pack_id_accepts_bare_names() -> None:
    assert d.safe_pack_id("waitrose") is True
    assert d.safe_pack_id("site_pack") is True
    assert d.safe_pack_id("a1") is True


def test_safe_pack_id_rejects_paths_and_traversal() -> None:
    assert d.safe_pack_id("") is False
    assert d.safe_pack_id("a/b") is False
    assert d.safe_pack_id("a\\b") is False
    assert d.safe_pack_id("..") is False
    assert d.safe_pack_id(".") is False
    assert d.safe_pack_id("../etc") is False
    assert d.safe_pack_id("foo/../bar") is False
    # A '..' sequence with NO path separator is still rejected.
    assert d.safe_pack_id("a..b") is False
    assert d.safe_pack_id("/abs") is False
    assert d.safe_pack_id(".hidden") is False


# --- peer_uid_allowed --------------------------------------------------------


def test_peer_uid_allowed_true_for_member() -> None:
    assert d.peer_uid_allowed(501, allowed={0, 501}) is True


def test_peer_uid_allowed_false_for_nonmember() -> None:
    assert d.peer_uid_allowed(999, allowed={0, 501}) is False


def test_peer_uid_allowed_root_configurable() -> None:
    # root is only allowed when explicitly in the allow-set.
    assert d.peer_uid_allowed(0, allowed={0, 501}) is True
    assert d.peer_uid_allowed(0, allowed={501}) is False
