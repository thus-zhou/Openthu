"""Cross-module integration checks for batch 3 (3.1 x 3.2 x 3.3).

These tests exercise the seams the module suites do not own:

* the account pseudonym module 3.3 requires is derived through module 2.2's
  public entry point, not by a caller-invented hash;
* a media state record is sealed with purpose MEDIA and is never accepted by
  module 3.1's ACTION-only compatibility reader;
* every error code module 3.1 can surface through a session/gateway result is
  registered in the frozen error-classification-v1 registry and mirrored in
  source_state.

Synthetic accounts, a synthetic PNG and temporary directories only; no campus,
model or business call is made.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
from pathlib import Path

import pytest
from PIL import Image

from learn_assistant.auth import _secure_unprotect
from thu_assistant import source_state
from thu_assistant.media_assets import MediaAssets
from thu_assistant.secure_store import (
    SecureAccountMismatch,
    SecureInputError,
    SecureStore,
    create_key_file,
    load_provider,
)

ROOT = Path(__file__).resolve().parents[1]
SCOPE_PATTERN = re.compile(r"^acct_[A-Za-z0-9_-]{16,64}$")


def _store(tmp_path: Path) -> SecureStore:
    backend = "windows-dpapi" if os.name == "nt" else "file-key"
    key_path = tmp_path / "keys" / "master.key"
    create_key_file(key_path, backend)
    return SecureStore(load_provider(key_path), tmp_path / "data")


def _png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), (12, 34, 56)).save(buffer, format="PNG")
    return buffer.getvalue()


def test_secure_store_exposes_the_pseudonym_media_requires(tmp_path):
    store = _store(tmp_path)

    scope = store.account_scope("2023000001", purpose="MEDIA")
    assert SCOPE_PATTERN.fullmatch(scope)
    assert scope == store.account_scope("2023000001", purpose="MEDIA")
    assert scope != store.account_scope("2023000002", purpose="MEDIA")
    # The pseudonym is purpose-scoped by design; the same account under a
    # different storage purpose is a different opaque token.
    assert scope != store.account_scope("2023000001", purpose="SESSION")
    assert store.account_scope("", purpose="MEDIA") == "none"

    with pytest.raises(SecureInputError):
        store.account_scope(None, purpose="MEDIA")  # type: ignore[arg-type]


def test_media_state_roundtrip_uses_the_store_pseudonym(tmp_path):
    store = _store(tmp_path)
    scope = store.account_scope("2023000001", purpose="MEDIA")
    image = tmp_path / "picture.png"
    image.write_bytes(_png())

    root = tmp_path / "temp"
    assets = MediaAssets(root, state_store=store)
    reference = assets.register_synthetic_file(
        image,
        account_scope=scope,
        task_id="task-batch3-integration",
        source_evidence_id="local.synthetic_fixture",
        retention="EXPLICIT_DELETE",
    )
    record_id = assets.state_record_id(scope)
    assets.close()

    reloaded = MediaAssets(root, state_store=store)
    assert reloaded.restore_state(scope) == 1
    data = reloaded.read_bytes(
        reference.media_id, account_scope=scope, task_id="task-batch3-integration"
    )
    assert hashlib.sha256(data).hexdigest() == reference.sha256

    other_scope = store.account_scope("2023000002", purpose="MEDIA")
    with pytest.raises(SecureAccountMismatch):
        store.read_json(
            record_id,
            purpose="MEDIA",
            account_domain="CAMPUS",
            service_domain="LOCAL",
            account=other_scope,
        )

    # Module 3.1's compatibility reader only accepts ACTION envelopes; a media
    # state record must never be returned through the action/todo path.
    record_file = next((tmp_path / "data").glob("records-*/*.secure"))
    assert _secure_unprotect(record_file.read_bytes()) is None
    reloaded.close()


def test_batch3_error_codes_are_registered_in_the_frozen_contract():
    javascript = "".join(
        (ROOT / "src" / "thu_assistant" / name).read_text(encoding="utf-8")
        for name in ("session-store.js", "session-policy.js")
    )
    codes = set(re.findall(r"fail\('([A-Z][A-Z0-9_]+)'", javascript))
    codes |= {
        "BROWSER_STATE_NOT_SAVED",
        "BROWSER_STATE_SYNC_FAILED",
        "HELPER_INVALID_RESPONSE",
        "HELPER_UNAVAILABLE",
        "LEARN_ACCOUNT_REQUIRED",
        "LEARN_SESSION_NOT_VERIFIED",
    }
    assert codes, "the session layer must expose at least one stable code"

    frozen = json.loads(
        (ROOT / "contracts" / "v0.2" / "error-classification.json").read_text(
            encoding="utf-8"
        )
    )
    missing = sorted(code for code in codes if code not in frozen["codes"])
    assert missing == []
    for code in sorted(codes):
        assert source_state.ERROR_CODES[code] == frozen["codes"][code]
