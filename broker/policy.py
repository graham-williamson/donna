"""HMAC, idempotency, rate limits, cooldown, approval codes, context sanitisation.

Spec: security-v1.1 §7.2 (idempotency key), §7.3 (HMAC), §7.4 (cooldown +
override), §7.7 (context_reason sanitisation), §13.2 (rate limits),
§11 (replay semantics), §9.1 (policy-check purity).

Invariant: **no network I/O anywhere in this module.** import-linter
enforces the no-network contract at the file boundary; this module
stays pure-Python-stdlib for everything it does.

HMAC canonical serialisation is explicit per §7.3:
  - separator \\x1f (ASCII unit separator, never valid inside any field)
  - integers as decimal strings (no leading zero, sign only for negatives)
  - timestamps as epoch-ms integers
  - strings as UTF-8 bytes

Verification order at every state transition (§7.3):
  1. Recompute params_hash from canonicalize(params_json).
     Mismatch → `audit.params_hash_mismatch` → integrity_failed.
  2. Verify HMAC over the full current field set.
     Mismatch → `audit.hmac_mismatch` → integrity_failed.
  3. Only then apply the transition.
"""
from __future__ import annotations

import hashlib
import hmac as _hmac
import re
import secrets
import time
from typing import Any, Optional

from broker import canonicalize
from broker import grants_db


# Unit separator (ASCII 0x1F). Never valid inside any covered field.
SEP = b"\x1f"

# §7.3 approval-code alphabet: RFC 4648 base32 minus I L O U.
# Remaining 32 symbols (~30 bits entropy at 6 chars).
APPROVAL_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTVWXYZ23456789"
APPROVAL_CODE_LENGTH = 6

# §7.4 default cooldown after denial.
DEFAULT_COOLDOWN_MINUTES = 30


# ---- idempotency key (§7.2) ---------------------------------------------


def idempotency_key(
    capability: str,
    canonical_params: bytes,
    date_component: str,
) -> str:
    """sha256 of capability || SEP || canonical_params || SEP || date_component.

    `canonical_params` must already be the RFC 8785 canonical bytes
    (see broker.canonicalize). `date_component` is per the capability's
    `idempotency_date_from` — either a UTC date string or
    `created_utc` computed by the caller.
    """
    if not isinstance(capability, str):
        raise TypeError("capability must be str")
    if not isinstance(canonical_params, (bytes, bytearray)):
        raise TypeError("canonical_params must be bytes")
    if not isinstance(date_component, str):
        raise TypeError("date_component must be str")
    h = hashlib.sha256()
    h.update(capability.encode("utf-8"))
    h.update(SEP)
    h.update(bytes(canonical_params))
    h.update(SEP)
    h.update(date_component.encode("utf-8"))
    return h.hexdigest()


# ---- approval code (§7.3) -----------------------------------------------


def generate_approval_code() -> str:
    """6 random characters from the 32-symbol alphabet. Uses `secrets`
    for CSPRNG-backed randomness; the caller is responsible for
    uniqueness via the partial unique index."""
    return "".join(
        secrets.choice(APPROVAL_CODE_ALPHABET)
        for _ in range(APPROVAL_CODE_LENGTH)
    )


# ---- HMAC serialisation and compute (§7.3) ------------------------------


def _encode_int(n: int) -> bytes:
    """Decimal string encoding, no leading zeros, sign only for negatives."""
    if not isinstance(n, int) or isinstance(n, bool):
        raise TypeError(f"expected int, got {type(n).__name__}")
    return str(n).encode("ascii")


def _encode_str(s: str) -> bytes:
    if not isinstance(s, str):
        raise TypeError(f"expected str, got {type(s).__name__}")
    return s.encode("utf-8")


def build_creation_message(
    request_id: str,
    capability: str,
    params_hash: str,
    idempotency_key_: str,
    risk_level: str,
    created_at: int,
    approval_expires_at: int,
) -> bytes:
    """Assemble the §7.3 creation-time HMAC message.

    Exposed publicly so the caller can reuse the exact bytes when
    computing the approval-time HMAC extension (which covers the
    same creation fields plus the fields that become immutable at
    approval).
    """
    return SEP.join([
        _encode_str(request_id),
        _encode_str(capability),
        _encode_str(params_hash),
        _encode_str(idempotency_key_),
        _encode_str(risk_level),
        _encode_int(created_at),
        _encode_int(approval_expires_at),
    ])


def compute_creation_hmac(
    key: bytes,
    request_id: str,
    capability: str,
    params_hash: str,
    idempotency_key_: str,
    risk_level: str,
    created_at: int,
    approval_expires_at: int,
) -> str:
    """HMAC-SHA256 hex digest over the creation-time immutable fields."""
    msg = build_creation_message(
        request_id=request_id,
        capability=capability,
        params_hash=params_hash,
        idempotency_key_=idempotency_key_,
        risk_level=risk_level,
        created_at=created_at,
        approval_expires_at=approval_expires_at,
    )
    return _hmac.new(key, msg, hashlib.sha256).hexdigest()


def compute_approval_hmac(
    key: bytes,
    creation_msg: bytes,
    execution_expires_at: int,
    approved_at: int,
) -> str:
    """Extend the creation message with the approval-time fields and
    recompute HMAC-SHA256. `creation_msg` is the output of
    `build_creation_message()` — reuse rather than reconstruct to
    guarantee serialisation parity."""
    msg = SEP.join([
        creation_msg,
        _encode_int(execution_expires_at),
        _encode_int(approved_at),
    ])
    return _hmac.new(key, msg, hashlib.sha256).hexdigest()


def verify_hmac(key: bytes, message: bytes, expected_hex: str) -> bool:
    """Constant-time compare. Returns False on any mismatch, including
    malformed hex length — never raises on attacker input."""
    computed = _hmac.new(key, message, hashlib.sha256).hexdigest()
    return _hmac.compare_digest(computed, expected_hex)


# ---- rate limits (§13.2) ------------------------------------------------


def rate_limit_check(
    conn: Any,
    capability: str,
    daily_cap: int,
    utc_date: str,
) -> bool:
    """Return True if under cap, False if exceeded. Counting is per
    (capability, UTC date). Denied/expired rows do NOT refund — the
    increment is caller-owned and happens at row creation, not at
    execution."""
    row = conn.execute(
        "SELECT count FROM rate_limits WHERE capability = ? AND date_utc = ?",
        (capability, utc_date),
    ).fetchone()
    current = int(row["count"]) if row is not None else 0
    return current < daily_cap


def rate_limit_increment(
    conn: Any, capability: str, utc_date: str
) -> None:
    """Upsert the counter. Atomic per the SQLite ON CONFLICT path."""
    with conn:
        conn.execute(
            "INSERT INTO rate_limits (capability, date_utc, count) "
            "VALUES (?, ?, 1) "
            "ON CONFLICT (capability, date_utc) DO UPDATE SET "
            "count = count + 1",
            (capability, utc_date),
        )


# ---- cooldown (§7.4) ----------------------------------------------------


def cooldown_remaining_seconds(
    conn: Any,
    idempotency_key_: str,
    cooldown_minutes: int = DEFAULT_COOLDOWN_MINUTES,
    now_ms: int | None = None,
) -> int:
    """For a denied row on this idempotency_key, return seconds remaining
    in the cooldown window. 0 when expired or when no denied row exists.

    `now_ms` is the caller's notion of 'now' — defaults to wall-clock.
    Exposed for deterministic tests.
    """
    row = conn.execute(
        "SELECT approval_expires_at, created_at, state FROM requests "
        "WHERE idempotency_key = ? AND state = 'denied' "
        "ORDER BY created_at DESC LIMIT 1",
        (idempotency_key_,),
    ).fetchone()
    if row is None:
        return 0

    # The denial moment itself isn't recorded as a field — we use
    # created_at as the cooldown anchor. Callers that need a tighter
    # denial-anchored window should extend the schema; for v1 the
    # spec's 30-minute window from creation is the contract.
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    deadline = int(row["created_at"]) + cooldown_minutes * 60 * 1000
    remaining_ms = deadline - now
    return max(0, remaining_ms // 1000)


# ---- context_reason sanitisation (§7.7) ---------------------------------


MAX_CONTEXT_REASON_LENGTH = 200

# §7.7 strip patterns. Order matters for audit reporting, not for
# correctness — each match becomes `[redacted]` regardless of which
# pattern fired first (the `redaction_types` list records all that hit).
_URL_RE = re.compile(r"https?://\S+")
_BASE64_RE = re.compile(r"[A-Za-z0-9+/=]{24,}")
_HEX_RE = re.compile(r"[A-Fa-f0-9]{24,}")
_DIGITS_RE = re.compile(r"\d{6,}")

# Non-ASCII that is not standard Latin + typographic punctuation.
# We keep: printable ASCII + common Latin-1 accented letters
# (À-ÿ excluding control chars) + curly quotes / en-dashes / em-dashes.
# Everything else (CJK, RTL overrides, zero-width joiners, etc.) strips.
_ALLOWED_CODEPOINT_RE = re.compile(
    r"[\x20-\x7e"           # printable ASCII
    r"\xa0-\u024f"          # Latin-1 Supplement + Latin Extended A/B
    r"\u2010-\u2019"        # hyphens + smart single quotes
    r"\u201c-\u201d"        # smart double quotes
    r"\u2013-\u2014"        # en-dash + em-dash
    r"]"
)


class ContextReasonTooLong(Exception):
    """Raised per §7.7 when `context_reason` exceeds the hard cap. The
    broker returns `{status: invalid_input, field: context_reason,
    reason: too long (max 200)}` to the caller."""


def sanitise_context_reason(raw: str) -> tuple[str, list[str]]:
    """Apply §7.7 ingest rules. Returns `(sanitised, redaction_types)`.

    Raises `ContextReasonTooLong` if `raw` exceeds the hard cap —
    length is a structural input error, not a sanitisation result.

    Redaction type names:
      - "url"
      - "base64"
      - "hex"
      - "digits"
      - "non_ascii"
    """
    if not isinstance(raw, str):
        raise TypeError(f"context_reason must be str, got {type(raw).__name__}")
    if len(raw) > MAX_CONTEXT_REASON_LENGTH:
        raise ContextReasonTooLong(
            f"context_reason length {len(raw)} exceeds max "
            f"{MAX_CONTEXT_REASON_LENGTH}"
        )

    redactions: list[str] = []

    # Tag every pattern that matches the ORIGINAL input — patterns
    # overlap (base64 is a superset of hex, for instance) and checking
    # after progressive substitution would hide the narrower match.
    pattern_order: list[tuple[str, re.Pattern[str]]] = [
        ("url", _URL_RE),
        ("base64", _BASE64_RE),
        ("hex", _HEX_RE),
        ("digits", _DIGITS_RE),
    ]
    for name, pat in pattern_order:
        if pat.search(raw):
            redactions.append(name)

    # Apply substitutions in a fixed order. Each pattern reduces its
    # matches to `[redacted]`; later patterns see the already-reduced
    # string, which is fine because `[redacted]` contains none of the
    # subsequent targets.
    result = raw
    for _name, pat in pattern_order:
        result = pat.sub("[redacted]", result)

    # Non-ASCII strip: reconstruct character-by-character so we catch
    # any codepoint outside the allowlist, not just ones matched by
    # a simple pattern.
    non_ascii_removed = False
    rebuilt_chars: list[str] = []
    for ch in result:
        if _ALLOWED_CODEPOINT_RE.fullmatch(ch):
            rebuilt_chars.append(ch)
        else:
            non_ascii_removed = True
    if non_ascii_removed:
        redactions.append("non_ascii")
        result = "".join(rebuilt_chars)

    return result, redactions


# ---- standing grants (broker-standing-grants §5, §6) --------------------
#
# A standing grant lets a specific (capability + pinned params) action
# auto-execute up to a rate limit. This block is the pure/deterministic
# policy core: it computes/verifies the constraints MAC, matches request
# params against a grant's pinned constraints, and (given `now` and a
# local DB connection) decides whether an active, in-rate grant covers a
# request. No network, no wall-clock — `now` is always an argument.

# §3.1 / §7: grant.create is the meta-privilege. It is hard-coded
# high-risk and is NEVER matched/authorised by any standing grant —
# grants cannot grant grants (no self-escalation).
GRANT_CREATE_CAPABILITY = "grant.create"

# Per-action-only capabilities (connected-sites-broker-handoff §2): money
# moves only with a fresh per-purchase human approval. These can never be
# covered by a standing grant — grant-create refuses to create one and
# check_standing_grants refuses to match one, so even a grant row smuggled
# into the store would be inert.
NO_STANDING_GRANTS = frozenset({
    "everyone_active.checkout",
})


class GrantConstraintError(Exception):
    """Raised when a grant's constraints are structurally invalid (e.g. a
    gmail.send grant that omits the mandatory `to` pin, §5)."""


def compute_constraints_mac(
    key: bytes, capability: str, constraints: Any
) -> str:
    """§5 constraints MAC: HMAC(broker_key, capability ‖ canonical(constraints)).

    Reuses broker.canonicalize (RFC 8785) for the constraints so a grant
    stored on disk can't be tampered with out-of-band, and the SEP
    separator from §7.3 so capability and constraints can never collide.
    `constraints` is the in-memory object (dict); it is canonicalised
    here so callers don't have to pre-serialise."""
    msg = (
        capability.encode("utf-8")
        + SEP
        + canonicalize.canonicalize(constraints)
    )
    return _hmac.new(key, msg, hashlib.sha256).hexdigest()


def verify_constraints_mac(key: bytes, grant: grants_db.StandingGrant) -> bool:
    """Recompute the constraints MAC from the grant's stored canonical
    constraints and constant-time compare against the stored MAC. False
    on any mismatch or malformed stored constraints — never raises on
    attacker-influenced data at rest."""
    try:
        import json
        constraints_obj = json.loads(grant.constraints)
    except Exception:
        return False
    expected = compute_constraints_mac(key, grant.capability, constraints_obj)
    return _hmac.compare_digest(expected, grant.constraints_mac)


def validate_constraints(capability: str, constraints: Any) -> None:
    """§5 structural validation of a grant's constraints, applied at
    grant-create time. Raises GrantConstraintError on violation.

    Rules:
      - constraints must be a JSON object.
      - For `gmail.send`, `to` is MANDATORY (no unpinned recipient is
        ever auto-sendable).
      - A `subject` pin, if present, must be either an exact string or
        a `{"prefix": <str>}` object.
    """
    if capability in NO_STANDING_GRANTS:
        raise GrantConstraintError(
            f"{capability} is per-action-only: every run requires a fresh "
            f"human approval, so no standing grant can cover it"
        )
    if not isinstance(constraints, dict):
        raise GrantConstraintError("constraints must be a JSON object")
    if capability == "gmail.send" and "to" not in constraints:
        raise GrantConstraintError(
            "gmail.send grants must pin `to` (no unpinned recipient is "
            "auto-sendable)"
        )
    if "subject" in constraints:
        subj = constraints["subject"]
        if isinstance(subj, dict):
            if set(subj.keys()) != {"prefix"} or not isinstance(
                subj.get("prefix"), str
            ):
                raise GrantConstraintError(
                    "subject pin object must be {\"prefix\": <string>}"
                )
        elif not isinstance(subj, str):
            raise GrantConstraintError(
                "subject pin must be a string or a {\"prefix\": ...} object"
            )


def constraints_match(constraints: Any, params: dict[str, Any]) -> bool:
    """§5 matching. Returns True iff `params` satisfies every pinned
    constraint. Fields not listed in `constraints` are free to vary.

    Pin kinds:
      - exact pin: a non-dict value. The param canonicalises identically
        to the pinned value (canonical-JSON equality, so list/string
        order and formatting are normalised, matching params_hash
        semantics).
      - prefix pin: `{"prefix": <str>}`. The param must be a string that
        startswith the prefix.
    """
    if not isinstance(constraints, dict):
        return False
    for field, pin in constraints.items():
        if field not in params:
            return False
        value = params[field]
        if isinstance(pin, dict) and "prefix" in pin:
            prefix = pin["prefix"]
            if not isinstance(value, str) or not isinstance(prefix, str):
                return False
            if not value.startswith(prefix):
                return False
        else:
            # Exact pin via canonical-JSON equality (same normalisation
            # the broker uses for params_hash).
            try:
                if canonicalize.canonicalize(pin) != canonicalize.canonicalize(
                    value
                ):
                    return False
            except Exception:
                return False
    return True


def within_rate(
    conn: Any, grant: grants_db.StandingGrant, now_ms: int
) -> bool:
    """§3.3 rolling-window rate check. True if the grant has been used
    fewer than `max_per_period` times within the last `period_seconds`
    ending at `now_ms`. Deterministic given `now_ms`."""
    window_start = now_ms - grant.period_seconds * 1000
    used = grants_db.count_uses_within(conn, grant.id, window_start)
    return used < grant.max_per_period


def check_standing_grants(
    conn: Any,
    capability: str,
    params: dict[str, Any],
    now_ms: int,
    broker_key: bytes,
) -> Optional[dict[str, Any]]:
    """§6 grant consultation — the pure policy step that runs BEFORE the
    risk-tier fallthrough.

    Returns an allow descriptor and records a use when an active grant
    matches; returns None otherwise (caller falls through to the existing
    low→allow / medium·high→approval / blocked→deny behaviour).

    Pure/deterministic: `now_ms` is an argument (no wall-clock); the
    grant store is read locally via `conn` (no network).

    Invariant (§3.1): `grant.create` is NEVER matched here — grants
    cannot grant grants. Short-circuits before touching the store.
    """
    if capability == GRANT_CREATE_CAPABILITY:
        return None
    if capability in NO_STANDING_GRANTS:
        # Per-action-only (e.g. checkout): never auto-authorised, even if
        # a grant row for it somehow exists in the store.
        return None
    for grant in grants_db.active_grants(conn, capability, now_ms):
        if not constraints_match(_loads_constraints(grant), params):
            continue
        if not verify_constraints_mac(broker_key, grant):
            continue
        if not within_rate(conn, grant, now_ms):
            continue
        grants_db.record_use(conn, grant.id, now_ms)
        return {
            "decision": "allow",
            "via": "standing_grant",
            "grant_id": grant.id,
            "risk_level": "high",
        }
    return None


def _loads_constraints(grant: grants_db.StandingGrant) -> Any:
    import json
    try:
        return json.loads(grant.constraints)
    except Exception:
        return None
