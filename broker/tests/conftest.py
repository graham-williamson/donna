"""Shared fixtures for broker tests.

Kept intentionally minimal in pre-flight; Ralph adds capability-specific
fixtures as each module gets implemented.
"""
from __future__ import annotations

import os
import pytest


@pytest.fixture
def broker_home(tmp_path):
    """Synthetic broker home dir with the subdir layout from §6 / §7.6 /
    §12.1, pointed at tmp_path. Tests that need real paths mutate under
    this root so nothing escapes the test's tmp_path.
    """
    home = tmp_path / "donna-broker"
    (home / ".config" / "donna" / "secrets").mkdir(parents=True)
    (home / ".config" / "donna" / "approval-queue").mkdir(parents=True)
    (home / ".config" / "donna" / "approval-responses").mkdir(parents=True)
    (home / ".config" / "donna" / "backups").mkdir(parents=True)
    (home / "audit").mkdir(parents=True)
    return home


@pytest.fixture
def hmac_key(broker_home):
    """32 random bytes at the spec path, mode 0400. §7.3."""
    key_path = broker_home / ".config" / "donna" / "hmac.key"
    key_path.write_bytes(os.urandom(32))
    key_path.chmod(0o400)
    return key_path
