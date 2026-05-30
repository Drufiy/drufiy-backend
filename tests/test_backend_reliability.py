import hashlib
import hmac

import pytest

from app.agent.pr_creator import (
    AuthError,
    PRCreationError,
    _create_branch,
    _pr_title,
    _raise_github_error,
    apply_unified_patch,
)
from app.agent.processor import _is_fix_branch_for_run
from app.config import settings
from app.webhook import _is_fix_branch, _strip_fix_branch_prefix, verify_signature


class _Resp:
    def __init__(self, status_code=201, text="", payload=None):
        self.status_code = status_code
        self.text = text
        self._payload = payload or {}

    def json(self):
        return self._payload


class _BranchClient:
    def __init__(self):
        self.refs = []

    async def post(self, url, json):
        self.refs.append(json["ref"])
        return _Resp(201)


def test_fix_branch_prefixes_are_backward_compatible():
    assert _is_fix_branch("prash/fix-run-12345678")
    assert _is_fix_branch("drufiy/fix-run-12345678")
    assert _strip_fix_branch_prefix("prash/fix-run-12345678-999") == "12345678-999"
    assert _strip_fix_branch_prefix("drufiy/fix-run-12345678") == "12345678"


def test_webhook_signature_validation_is_strict():
    body = b'{"action":"completed"}'
    valid = "sha256=" + hmac.new(
        settings.github_webhook_secret.encode(),
        body,
        hashlib.sha256,
    ).hexdigest()

    assert verify_signature(body, valid)
    assert not verify_signature(body, None)
    assert not verify_signature(body, "sha256=bad")


def test_dedupe_only_matches_same_run_prefix():
    run_id = "12345678-aaaa-bbbb-cccc-123456789012"
    assert _is_fix_branch_for_run("prash/fix-run-12345678", run_id)
    assert _is_fix_branch_for_run("drufiy/fix-run-12345678-999", run_id)
    assert not _is_fix_branch_for_run("prash/fix-run-87654321", run_id)


@pytest.mark.asyncio
async def test_create_branch_uses_configured_prash_prefix():
    client = _BranchClient()
    branch = await _create_branch(
        client,
        "Drufiy/example",
        "12345678-aaaa-bbbb-cccc-123456789012",
        "abc123",
    )
    assert branch == "prash/fix-run-12345678"
    assert client.refs == ["refs/heads/prash/fix-run-12345678"]


def test_unified_patch_rejects_context_mismatch():
    current = "one\ntwo\nthree\n"
    patch = "@@ -1,3 +1,3 @@\n one\n-wrong\n+TWO\n three\n"
    with pytest.raises(PRCreationError, match="does not match"):
        apply_unified_patch(current, patch)


def test_github_401_is_auth_error():
    resp = _Resp(status_code=401, text="bad credentials")
    with pytest.raises(AuthError, match="invalid or expired"):
        _raise_github_error(resp, "commit file")


def test_github_rate_limit_is_actionable_error():
    resp = _Resp(status_code=403, text="API rate limit exceeded")
    with pytest.raises(PRCreationError, match="rate limit exceeded"):
        _raise_github_error(resp, "commit file")


def test_speculative_pr_title_is_tagged():
    title = _pr_title({"speculative": True, "problem_summary": "CI dependency failure"})
    assert title.startswith("[SPECULATIVE] fix: CI dependency failure")
