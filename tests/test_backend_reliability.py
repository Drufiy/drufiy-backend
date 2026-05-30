import pytest

from app.agent.pr_creator import PRCreationError, _create_branch, apply_unified_patch
from app.agent.processor import _is_fix_branch_for_run
from app.webhook import _is_fix_branch, _strip_fix_branch_prefix


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
