# promoter_daemon.py
"""Root Unix-socket promoter daemon (design §6.2, §9.5).

A minimal, blocking, single-operation Unix-domain-socket server that fronts the
``promoter.install`` orchestrator. It is the privileged install boundary, so the
surface is deliberately tiny and fail-closed:

  * ONE operation — ``install_pack``. Anything else is an error reply.
  * Length-prefixed frames — a 4-byte big-endian ``uint32`` length, then a JSON
    body. Oversized declared lengths are rejected WITHOUT allocating.
  * Peer-credential check — only the broker user (``donna-broker``) or root may
    connect; everyone else is closed on. (Darwin: ``os.getpeereid``.)
  * Socket created mode 0600.

Testability split:
  * ``handle_frame`` — PURE framing + dispatch over a single request; the
    install effect is injected. NEVER raises — every error becomes an error
    reply. This carries the real coverage (incl. adversarial inputs).
  * ``peer_uid_allowed`` — PURE allow-set membership.
  * ``serve`` — the accept loop. The unavoidable OS/socket calls are
    ``# pragma: no cover`` (Graham smoke-tests the live daemon).
  * ``main`` — privileged wiring (real resources), ``# pragma: no cover``;
    kept correct, not unit-tested.

After a successful merge the daemon PUBLISHES the merged manifest into the
broker's config dir (``--config-dir``, e.g. ``/Users/donna-broker/.config/donna``)
via ``promoter_fs.publish_to_config`` — a per-file atomic copy of ONLY the
manifest artifacts (capabilities.yaml, mcp-tools.yaml, schemas/, profiles/). It
NEVER touches the requests DB or the age vault that also live in that dir. There
is no broker *service* to restart: the broker is a per-call CLI that reloads
capabilities.yaml on its next invocation, so publish (not a launchctl kickstart)
is the real post-merge action.

The daemon NEVER reads credentials or the age vault — it only re-verifies and
merges signed packs via the orchestrator, then publishes the manifest artifacts.
"""
from __future__ import annotations

import json
import os
import socket
import struct
import sys
from pathlib import Path
from typing import Any, Callable, Protocol

DEFAULT_MAX_FRAME = 65536


class _DoInstall(Protocol):
    def __call__(  # pragma: no cover - Protocol stub
        self, *, pack_id: str
    ) -> dict[str, str]: ...


def safe_pack_id(pack_id: str) -> bool:
    """PURE: is this a SAFE bare pack directory name?

    The frame carries only a ``pack_id``; ``main`` resolves the pack dir as
    ``<packs_dir>/<pack_id>``. A pack_id must therefore be a bare directory
    name — never a path. Reject anything containing a path separator (``/`` or
    ``\\``), a ``..`` traversal component, a leading dot, or that is empty.
    Defence in depth against a hostile client trying to escape the packs dir.
    """
    if not pack_id:
        return False
    if "/" in pack_id or "\\" in pack_id:
        return False
    if pack_id in (".", ".."):
        return False
    if ".." in pack_id:
        return False
    if pack_id.startswith("."):
        return False
    return True


def _reply(obj: dict[str, Any]) -> bytes:
    """Encode a reply object as a length-prefixed JSON frame."""
    body = json.dumps(obj).encode("utf-8")
    return struct.pack(">I", len(body)) + body


def _error(message: str) -> bytes:
    return _reply({"ok": False, "error": message})


def handle_frame(
    raw: bytes,
    *,
    do_install: _DoInstall,
    max_frame: int = DEFAULT_MAX_FRAME,
) -> bytes:
    """Parse ONE length-prefixed JSON frame, dispatch ``install_pack``, and
    return a length-prefixed JSON reply.

    PURE and total: it NEVER raises. Every malformed input, unknown op, or
    ``do_install`` exception becomes ``{"ok": false, "error": ...}``. Success is
    ``{"ok": true, ...result}``. ``do_install`` is called ONLY when the frame is
    a well-formed ``install_pack`` request with a string ``pack_id`` that is a
    SAFE bare directory name (``safe_pack_id``) — a traversal pack_id is
    rejected here WITHOUT calling do_install (defence in depth: nothing
    privileged runs on a bad frame). The frame carries NEITHER a request_id NOR
    a client-supplied pack_dir: the promoter resolves the pack dir from the
    pack_id and the approval from the pack identity it re-verifies.
    """
    # 1. Length prefix.
    if len(raw) < 4:
        return _error("short read: missing length prefix")
    (declared,) = struct.unpack(">I", raw[:4])
    if declared > max_frame:
        return _error(f"frame too large: {declared} > max_frame {max_frame}")
    body = raw[4:]
    if len(body) < declared:
        return _error("short read: body shorter than declared length")
    body = body[:declared]

    # 2. JSON object.
    try:
        parsed = json.loads(body)
    except (ValueError, UnicodeDecodeError) as e:
        return _error(f"bad JSON body: {e}")
    if not isinstance(parsed, dict):
        return _error("frame body must be a JSON object")

    # 3. Dispatch — single op only.
    op = parsed.get("op")
    if op != "install_pack":
        return _error(f"unknown op: {op!r}")

    pack_id = parsed.get("pack_id")
    if not isinstance(pack_id, str) or not pack_id:
        return _error("missing or invalid field: pack_id")
    if not safe_pack_id(pack_id):
        # Reject a traversal / path-bearing pack_id at the validation layer —
        # do_install is NEVER called for it.
        return _error(f"unsafe pack_id: {pack_id!r}")

    # 4. Run the (injected) privileged install. Any failure -> error reply.
    try:
        result = do_install(pack_id=pack_id)
    except Exception as e:  # noqa: BLE001 — fail-closed: every error is a reply.
        return _error(str(e))

    reply: dict[str, Any] = {"ok": True}
    reply.update(result)
    return _reply(reply)


def peer_uid_allowed(uid: int, *, allowed: set[int]) -> bool:
    """PURE: is this peer UID permitted to use the promoter socket?

    ``allowed`` is the configured set (the broker user, and optionally root).
    Root (0) is allowed ONLY when explicitly present — nothing is implicit.
    """
    return uid in allowed


def _read_frame(conn: socket.socket, max_frame: int) -> bytes:  # pragma: no cover - socket I/O, smoke-tested live
    """Read one length-prefixed frame off the socket (length-checked before
    allocating). Returns the raw 4-byte-prefix + body so handle_frame parses it
    uniformly. Bounded by ``max_frame`` to avoid a huge read on a hostile peer."""
    prefix = conn.recv(4)
    if len(prefix) < 4:
        return prefix  # handle_frame turns a short prefix into an error reply
    (declared,) = struct.unpack(">I", prefix[:4])
    if declared > max_frame:
        return prefix  # don't read the oversized body; handle_frame rejects it
    body = b""
    while len(body) < declared:
        chunk = conn.recv(declared - len(body))
        if not chunk:
            break
        body += chunk
    return prefix + body


def serve(
    sock_path: str,
    *,
    do_install: _DoInstall,
    allowed_uids: set[int],
    max_frame: int = DEFAULT_MAX_FRAME,
) -> None:  # pragma: no cover - blocking accept loop + OS/socket calls, smoke-tested live
    """Create the 0600 Unix socket and serve install requests one at a time.

    Peer-cred checked (``os.getpeereid`` on Darwin); disallowed peers are closed
    on without a reply. Each accepted connection yields exactly one frame ->
    one reply -> close. Blocks forever. Every line here is an unavoidable OS or
    socket call, hence the module-level pragma; the security logic it calls
    (handle_frame, peer_uid_allowed) is fully unit-tested."""
    if os.path.exists(sock_path):
        os.unlink(sock_path)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(sock_path)
    os.chmod(sock_path, 0o600)  # owner-only — defence in depth atop peer-cred
    srv.listen(8)
    try:
        while True:
            conn, _ = srv.accept()
            try:
                uid, _gid = os.getpeereid(conn.fileno())  # type: ignore[attr-defined]  # Darwin-only; stubs omit it
                if not peer_uid_allowed(uid, allowed=allowed_uids):
                    continue  # reject silently — closed in finally
                raw = _read_frame(conn, max_frame)
                conn.sendall(handle_frame(raw, do_install=do_install, max_frame=max_frame))
            finally:
                conn.close()
    finally:
        srv.close()
        if os.path.exists(sock_path):
            os.unlink(sock_path)


def main(argv: list[str]) -> int:  # pragma: no cover - privileged wiring; opens real resources
    """Wire the real ``do_install`` and serve. Config from argv/env.

    Resolves ``pack_dir`` defensively from the client-supplied ``pack_id``: the
    frame carries ONLY a bare ``pack_id`` (validated by ``safe_pack_id`` in
    handle_frame), and the orchestrator is given ``<packs_dir>/<pack_id>`` — the
    client never supplies a path. The approval is resolved by pack identity
    (the orchestrator reads the broker DB itself), not by a client request_id.
    """
    import argparse

    from broker import promoter, promoter_fs, promoter_ledger, requests_db

    parser = argparse.ArgumentParser(prog="promoter_daemon")
    parser.add_argument("--socket", default=os.environ.get("DONNA_PROMOTER_SOCKET", "/var/run/donna/promoter.sock"))
    parser.add_argument("--packs-dir", default=os.environ.get("DONNA_PROMOTER_PACKS_DIR", "/Users/donna-broker/broker/packs/available"))
    parser.add_argument("--trusted-keys-dir", default=os.environ.get("DONNA_PROMOTER_TRUSTED_KEYS_DIR", "/etc/donna/promoter/trusted_keys"))
    parser.add_argument("--live-manifests-dir", default=os.environ.get("DONNA_PROMOTER_LIVE_MANIFESTS_DIR", "/Users/donna-broker/broker/manifests"))
    parser.add_argument("--config-dir", default=os.environ.get("DONNA_PROMOTER_CONFIG_DIR", "/Users/donna-broker/.config/donna"))
    parser.add_argument("--broker-db", default=os.environ.get("DONNA_PROMOTER_BROKER_DB", "/Users/donna-broker/.config/donna/requests.db"))
    parser.add_argument("--ledger", default=os.environ.get("DONNA_PROMOTER_LEDGER", "/var/log/donna/promoter.jsonl"))
    args = parser.parse_args(argv)

    packs_root = Path(args.packs_dir).resolve()
    conn = requests_db.open_db(args.broker_db)
    approvals = promoter.RequestsDbApprovalSource(conn)
    ledger = promoter_ledger.Ledger(args.ledger, now=__import__("time").time)

    def do_install(*, pack_id: str) -> dict[str, str]:
        # handle_frame already validated pack_id is a safe bare name; resolve it
        # under the configured packs root. Never trust a client-supplied path.
        candidate = (packs_root / pack_id).resolve()
        if candidate.parent != packs_root:
            raise promoter.PromoterError(
                f"resolved pack dir escapes packs dir: {pack_id}"
            )
        # Post-merge action: PUBLISH the merged manifest from the manifests-only
        # live dir into the dir the broker reads (--config-dir). This is a
        # per-file atomic copy of ONLY capabilities.yaml + mcp-tools.yaml +
        # schemas/ + profiles/ — it never touches the requests DB or the age
        # vault that also live in the config dir. There is no broker service to
        # restart: the per-call CLI reloads capabilities.yaml on next invocation.
        return promoter.install(
            pack_dir=str(candidate),
            trusted_keys_dir=args.trusted_keys_dir,
            live_manifests_dir=args.live_manifests_dir,
            approvals=approvals,
            publish=lambda: promoter_fs.publish_to_config(
                args.live_manifests_dir, args.config_dir
            ),
            ledger=ledger,
            now=__import__("time").time,
        )

    # Allowed peers: the broker user (resolved by name) and root.
    allowed: set[int] = {0}
    try:
        import pwd

        allowed.add(pwd.getpwnam("donna-broker").pw_uid)
    except KeyError:
        pass

    serve(args.socket, do_install=do_install, allowed_uids=allowed)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
