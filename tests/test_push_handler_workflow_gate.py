"""
Security regression test: push_handler.py's pre-emptive fix path
(handle_push_event) creates PRs via create_fix_pr() directly, bypassing
_apply_fix()'s workflow-file safety gate in processor.py entirely. Found
while investigating an unrelated July 30 mystery (why one workflow-file
fix got a PR and another didn't) — this path had NO equivalent check,
so a diagnosis whose files_changed touched .github/workflows/* would
auto-create a PR without the same "never auto-apply, workflow files
have privileged secrets access" protection _apply_fix() enforces.

No real I/O: supabase and the GitHub-calling helpers are faked/mocked.
"""
import pytest

from app.agent import push_handler
from app.agent.schemas import Diagnosis, FileChange


class _Query:
    def __init__(self, table_name, store):
        self._table = table_name
        self._store = store
        self._filters = {}

    def select(self, *a, **kw):
        return self

    def eq(self, key, val):
        self._filters[key] = val
        return self

    def limit(self, *a, **kw):
        return self

    def insert(self, row):
        self._insert_row = row
        return self

    def update(self, row):
        self._store.setdefault("updates", []).append((self._table, row))
        return self

    def execute(self):
        class R:
            pass
        r = R()
        if self._table == "connected_repos":
            r.data = [{"id": "repo-1", "repo_full_name": "acme/widgets", "default_branch": "main"}]
        elif hasattr(self, "_insert_row"):
            r.data = [{"id": f"{self._table}-inserted-id"}]
        else:
            r.data = []
        return r


class _FakeSupabase:
    def __init__(self, store):
        self._store = store

    def table(self, name):
        return _Query(name, self._store)


WORKFLOW_DIAGNOSIS = Diagnosis(
    problem_summary="Syntax error introduced",
    root_cause="Missing colon after function definition",
    fix_description="Add the missing colon and also fix the CI workflow's Python version pin",
    fix_type="safe_auto_apply",
    confidence=0.9,
    category="code",
    files_changed=[
        FileChange(path="app/main.py", new_content="def f():\n    pass\n", explanation="fix syntax"),
        FileChange(path=".github/workflows/ci.yml", new_content="name: CI\n", explanation="bump python version"),
    ],
)

PUSH_PAYLOAD = {
    "repository": {"full_name": "acme/widgets"},
    "ref": "refs/heads/main",
    "after": "deadbeef",
    "head_commit": {"message": "oops"},
    "commits": [{"added": [], "modified": ["app/main.py"]}],
}


@pytest.mark.asyncio
async def test_workflow_file_change_never_auto_creates_pr_from_push_preflight(monkeypatch):
    store: dict = {}
    create_fix_pr_called = False

    async def fake_create_fix_pr(**kwargs):
        nonlocal create_fix_pr_called
        create_fix_pr_called = True
        return {"branch": "prash/fix", "pr_url": "https://github.com/x/y/pull/1", "pr_number": 1}

    async def fake_get_repo_access_token(repo):
        return "tok"

    async def fake_fetch_changed_files(**kwargs):
        return {"app/main.py": "def f(\n    pass\n"}  # real syntax error

    async def fake_fetch_commit_diff(*a, **kw):
        return "diff"

    async def fake_diagnose_failure(**kwargs):
        return WORKFLOW_DIAGNOSIS

    monkeypatch.setattr(push_handler, "supabase", _FakeSupabase(store))
    monkeypatch.setattr(push_handler, "create_fix_pr", fake_create_fix_pr)
    monkeypatch.setattr(push_handler, "get_repo_access_token", fake_get_repo_access_token)
    monkeypatch.setattr(push_handler, "_fetch_changed_files", fake_fetch_changed_files)
    monkeypatch.setattr(push_handler, "_fetch_commit_diff", fake_fetch_commit_diff)
    monkeypatch.setattr(push_handler, "diagnose_failure", fake_diagnose_failure)

    await push_handler.handle_push_event(PUSH_PAYLOAD)

    assert create_fix_pr_called is False, "workflow-file diagnosis must never auto-create a PR from push preflight"
    ci_run_updates = [row for table, row in store.get("updates", []) if table == "ci_runs"]
    assert any(u.get("status") == "diagnosed" for u in ci_run_updates)
    diagnosis_updates = [row for table, row in store.get("updates", []) if table == "diagnoses"]
    assert any(u.get("fix_type") == "review_recommended" for u in diagnosis_updates)


@pytest.mark.asyncio
async def test_non_workflow_fix_still_creates_pr_normally(monkeypatch):
    """Control: a diagnosis that never touches .github/workflows/* is unaffected by the new gate."""
    store: dict = {}
    create_fix_pr_called = False

    async def fake_create_fix_pr(**kwargs):
        nonlocal create_fix_pr_called
        create_fix_pr_called = True
        return {"branch": "prash/fix", "pr_url": "https://github.com/x/y/pull/1", "pr_number": 1}

    async def fake_get_repo_access_token(repo):
        return "tok"

    async def fake_fetch_changed_files(**kwargs):
        return {"app/main.py": "def f(\n    pass\n"}

    async def fake_fetch_commit_diff(*a, **kw):
        return "diff"

    code_only_diagnosis = WORKFLOW_DIAGNOSIS.model_copy(update={
        "files_changed": [FileChange(path="app/main.py", new_content="def f():\n    pass\n", explanation="fix syntax")],
    })

    async def fake_diagnose_failure(**kwargs):
        return code_only_diagnosis

    monkeypatch.setattr(push_handler, "supabase", _FakeSupabase(store))
    monkeypatch.setattr(push_handler, "create_fix_pr", fake_create_fix_pr)
    monkeypatch.setattr(push_handler, "get_repo_access_token", fake_get_repo_access_token)
    monkeypatch.setattr(push_handler, "_fetch_changed_files", fake_fetch_changed_files)
    monkeypatch.setattr(push_handler, "_fetch_commit_diff", fake_fetch_commit_diff)
    monkeypatch.setattr(push_handler, "diagnose_failure", fake_diagnose_failure)

    await push_handler.handle_push_event(PUSH_PAYLOAD)

    assert create_fix_pr_called is True
