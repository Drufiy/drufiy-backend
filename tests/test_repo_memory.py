from app.agent import repo_memory


class FakeQuery:
    def __init__(self, table_name, data):
        self.table_name = table_name
        self.data = data
        self.filters = []
        self._limit = None

    def select(self, *_args, **_kwargs):
        return self

    def eq(self, key, value):
        self.filters.append(("eq", key, value))
        return self

    def in_(self, key, values):
        self.filters.append(("in", key, set(values)))
        return self

    def order(self, *_args, **_kwargs):
        return self

    def limit(self, value):
        self._limit = value
        return self

    def execute(self):
        rows = list(self.data.get(self.table_name, []))
        for op, key, value in self.filters:
            if op == "eq":
                rows = [r for r in rows if r.get(key) == value]
            elif op == "in":
                rows = [r for r in rows if r.get(key) in value]
        if self._limit is not None:
            rows = rows[: self._limit]
        return type("Resp", (), {"data": rows})()


class FakeSupabase:
    def __init__(self, data):
        self.data = data

    def table(self, table_name):
        return FakeQuery(table_name, self.data)


def test_build_repo_memory_collects_repo_specific_context(monkeypatch):
    monkeypatch.setattr(
        repo_memory,
        "supabase",
        FakeSupabase(
            {
                "ci_runs": [
                    {"id": "run-1", "repo_id": "repo-1", "status": "verified", "created_at": "2026-07-01T00:00:00Z"},
                    {"id": "run-2", "repo_id": "repo-1", "status": "failed", "created_at": "2026-07-02T00:00:00Z"},
                    {"id": "run-3", "repo_id": "repo-2", "status": "verified", "created_at": "2026-07-03T00:00:00Z"},
                ],
                "diagnoses": [
                    {
                        "id": "diag-1",
                        "run_id": "run-1",
                        "problem_summary": "React peer dependency conflict",
                        "root_cause": "react-dom and @types/react were out of sync",
                        "fix_description": "Bumped react-dom and type packages together",
                        "category": "dependency",
                        "confidence": 0.95,
                        "files_changed": [{"path": "package.json"}],
                        "verification_status": "verified",
                        "error_signature": "sig-a",
                        "pr_merged_at": "2026-07-01T00:05:00Z",
                        "created_at": "2026-07-01T00:01:00Z",
                    },
                    {
                        "id": "diag-2",
                        "run_id": "run-2",
                        "problem_summary": "Same failure again",
                        "root_cause": "Wrong hypothesis",
                        "fix_description": "Changed a different package",
                        "category": "dependency",
                        "confidence": 0.85,
                        "files_changed": [{"path": "package.json"}],
                        "verification_status": "failed",
                        "error_signature": "sig-a",
                        "pr_closed_without_merge": True,
                        "created_at": "2026-07-02T00:01:00Z",
                    },
                ],
                "flaky_tests": [
                    {
                        "repo_id": "repo-1",
                        "test_file": "tests/test_api.py",
                        "test_name": "test_timeout",
                        "fail_count": 3,
                        "pass_after_retry_count": 2,
                        "is_active": True,
                    }
                ],
                "known_good_files": [
                    {"repo_id": "repo-1", "file_path": ".github/workflows/ci.yml", "verified_at": "2026-07-01T00:00:00Z"}
                ],
            }
        ),
    )

    memory = repo_memory.build_repo_memory("repo-1")

    assert memory.similar_fixes[0]["problem_summary"] == "React peer dependency conflict"
    assert memory.repeated_error_signatures[0]["error_signature"] == "sig-a"
    assert memory.category_outcomes["dependency"]["attempts"] == 2
    assert memory.category_outcomes["dependency"]["verified"] == 1
    assert memory.flaky_tests[0]["test_file"] == "tests/test_api.py"
    assert memory.known_good_files[0]["file_path"] == ".github/workflows/ci.yml"

    prompt = memory.as_prompt_context()
    assert "REPO MEMORY" in prompt
    assert "React peer dependency conflict" in prompt
    assert "Known flaky tests" in prompt
