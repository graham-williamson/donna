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
  * ``_kickstart_broker`` / ``main`` — privileged wiring (subprocess + real
    resources), ``# pragma: no cover``; kept correct, not unit-tested.

The daemon NEVER reads credentials or the age vault — it only re-verifies and
merges signed packs via the orchestrator.
"""
from __future__ import annotations

import json
import os
import socket
import struct
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Protocol

DEFAULT_MAX_FRAME = 65536

# Best-known broker launchd label. There is currently NO long-running broker
# launchd *service* — the broker is invoked per-call as the CLI
# ``/usr/local/bin/donna-broker`` (sudo), and the only loaded ``com.donna.broker.*``
# job is the daily ``verify-audit`` cron. This constant names the service the
# promoter would kickstart once the broker runs as a resident daemon; it matches
# the plan's stated best-known value. Override via the ``--broker-label`` /
# ``DONNA_BROKER_LABEL`` config in main(). NOTE: confirm against the real
# LaunchDaemon label before deploy.
BROKER_LAUNCHD_LABEL = "com.user.daru-broker"


class _DoInstall(Protocol):
    def __call__(  # pragma: no cover - Protocol stub
        self, *, pack_dir: str, request_id: str
    ) -> dict[str, str]: ...


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
    a well-formed ``install_pack`` request with string ``pack_dir`` +
    ``request_id`` (defence in depth: nothing privileged runs on a bad frame).
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

    pack_dir = parsed.get("pack_dir")
    request_id = parsed.get("request_id")
    if not isinstance(pack_dir, str) or not pack_dir:
        return _error("missing or invalid field: pack_dir")
    if not isinstance(request_id, str) or not request_id:
        return _error("missing or invalid field: request_id")

    # 4. Run the (injected) privileged install. Any failure -> error reply.
    try:
        result = do_install(pack_dir=pack_dir, request_id=request_id)
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


def _kickstart_broker(label: str = BROKER_LAUNCHD_LABEL) -> None:  # pragma: no cover - launchctl subprocess, smoke-tested live
    """Restart the broker so it reloads the freshly-merged manifests.

    Runs ``launchctl kickstart -k system/<label>`` (the promoter runs as a root
    LaunchDaemon, so the broker target lives in the ``system`` domain). The only
    place this module touches ``subprocess`` — hence the ``.importlinter``
    allowance. Raises ``CalledProcessError`` on failure; the orchestrator treats
    a restart failure as ``installed_restart_failed`` (merge stands)."""
    subprocess.run(
        ["launchctl", "kickstart", "-k", f"system/{label}"],
        check=True,
        capture_output=True,
    )


def main(argv: list[str]) -> int:  # pragma: no cover - privileged wiring; opens real resources
    """Wire the real ``do_install`` and serve. Config from argv/env.

    Resolves ``pack_dir`` defensively: the orchestrator is given the configured
    packs dir + the request's ``pack_id`` (read from the broker DB), NOT a
    client-supplied absolute path — a client-supplied ``pack_dir`` is accepted
    only if it is a direct child of the configured packs dir.
    """
    import argparse

    from broker import promoter, promoter_ledger, requests_db

    parser = argparse.ArgumentParser(prog="promoter_daemon")
    parser.add_argument("--socket", default=os.environ.get("DONNA_PROMOTER_SOCKET", "/var/run/donna/promoter.sock"))
    parser.add_argument("--packs-dir", default=os.environ.get("DONNA_PROMOTER_PACKS_DIR", "/Users/donna-broker/broker/packs/available"))
    parser.add_argument("--trusted-keys-dir", default=os.environ.get("DONNA_PROMOTER_TRUSTED_KEYS_DIR", "/etc/donna/promoter/trusted_keys"))
    parser.add_argument("--live-manifests-dir", default=os.environ.get("DONNA_PROMOTER_LIVE_MANIFESTS_DIR", "/Users/donna-broker/broker/manifests"))
    parser.add_argument("--broker-db", default=os.environ.get("DONNA_PROMOTER_BROKER_DB", "/Users/donna-broker/.config/donna/requests.db"))
    parser.add_argument("--ledger", default=os.environ.get("DONNA_PROMOTER_LEDGER", "/var/log/donna/promoter.jsonl"))
    parser.add_argument("--broker-label", default=os.environ.get("DONNA_BROKER_LABEL", BROKER_LAUNCHD_LABEL))
    args = parser.parse_args(argv)

    packs_root = Path(args.packs_dir).resolve()
    conn = requests_db.open_db(args.broker_db)
    approvals = promoter.RequestsDbApprovalSource(conn)
    ledger = promoter_ledger.Ledger(args.ledger, now=__import__("time").time)

    def do_install(*, pack_dir: str, request_id: str) -> dict[str, str]:
        # Defence in depth: never trust a client-supplied absolute path. The
        # pack dir MUST be a direct child of the configured packs root.
        candidate = Path(pack_dir).resolve()
        if candidate.parent != packs_root:
            raise promoter.PromoterError(
                f"pack_dir not a direct child of packs dir: {pack_dir}"
            )
        return promoter.install(
            pack_dir=str(candidate),
            request_id=request_id,
            trusted_keys_dir=args.trusted_keys_dir,
            live_manifests_dir=args.live_manifests_dir,
            approvals=approvals,
            restart=lambda: _kickstart_broker(args.broker_label),
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
