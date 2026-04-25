import pytest

from app.agent.workflow_diff import DiffRisk, assess_diff_risk

REPO_ID = "00000000-0000-0000-0000-000000000000"  # fake — no DB needed for these unit tests


@pytest.mark.asyncio
async def test_identical_content(monkeypatch):
    content = "name: CI\non: push\njobs:\n  test:\n    runs-on: ubuntu-latest\n"
    monkeypatch.setattr(
        "app.agent.workflow_diff.supabase",
        _mock_supabase(content),
    )
    risk = await assess_diff_risk(REPO_ID, ".github/workflows/ci.yml", content)
    assert risk.risk_level == "low"
    assert risk.changed_regions == 0


@pytest.mark.asyncio
async def test_one_line_change(monkeypatch):
    old = "name: CI\non: push\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v3\n      - uses: actions/setup-node@v3\n        with:\n          node-version: '14'\n"
    new = old.replace("node-version: '14'", "node-version: '20'")
    monkeypatch.setattr("app.agent.workflow_diff.supabase", _mock_supabase(old))
    risk = await assess_diff_risk(REPO_ID, ".github/workflows/ci.yml", new)
    assert risk.risk_level == "low"
    assert risk.changed_regions == 1


@pytest.mark.asyncio
async def test_no_known_good(monkeypatch):
    monkeypatch.setattr("app.agent.workflow_diff.supabase", _mock_supabase(None))
    risk = await assess_diff_risk(REPO_ID, ".github/workflows/new.yml", "content: here")
    assert risk.has_known_good is False
    assert risk.risk_level == "medium"


@pytest.mark.asyncio
async def test_massive_rewrite(monkeypatch):
    old = "\n".join(f"line {i}" for i in range(50))
    new = "\n".join(f"rewritten {i}" for i in range(50))
    monkeypatch.setattr("app.agent.workflow_diff.supabase", _mock_supabase(old))
    risk = await assess_diff_risk(REPO_ID, "some/file.yml", new)
    assert risk.risk_level == "high"


# ── Minimal mock ─────────────────────────────────────────────────────────────

class _MockQuery:
    def __init__(self, content):
        self._content = content

    def select(self, *a, **kw):
        return self

    def eq(self, *a, **kw):
        return self

    def limit(self, *a, **kw):
        return self

    def execute(self):
        class R:
            pass
        r = R()
        r.data = [{"content": self._content}] if self._content is not None else []
        return r


class _MockSupabase:
    def __init__(self, content):
        self._content = content

    def table(self, *a):
        return _MockQuery(self._content)


def _mock_supabase(content):
    return _MockSupabase(content)
