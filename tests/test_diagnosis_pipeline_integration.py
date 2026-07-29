"""
M5: integration test tying M1 (repo memory), M3 (confidence recalibration), and
M4 (dependency chain completeness) together through the real diagnose_failure()
entry point — not just their individual unit tests in isolation. No API calls,
no Supabase: the model call is faked, same pattern as test_diagnosis_guardrails.py.
"""
import json

import pytest

from app.agent.diagnosis_agent import diagnose_failure
from app.agent.repo_memory import RepoMemory


def _poor_track_record_repo_memory() -> RepoMemory:
    memory = RepoMemory(repo_id="repo-1")
    memory.category_outcomes = {
        # 1/5 verified, and 2 of those got reverted -> effective rate 0 -> ceiling 0.2
        "dependency": {
            "attempts": 5,
            "verified": 3,
            "failed": 2,
            "rejected": 0,
            "reverted": 3,
            "exhausted": 2,
            "verified_rate": 0.6,
        }
    }
    return memory


def _partial_peer_bump_raw_args() -> dict:
    manifest = {
        "dependencies": {"react": "^18.2.0", "react-dom": "^18.2.0"},
        "devDependencies": {"@types/react": "^17.0.39", "@types/react-dom": "^18.2.0"},
    }
    return {
        "problem_summary": "React peer dependency conflict on CI",
        "root_cause": "react-dom requires react ^18 but react was pinned to ^17",
        "fix_description": "Bump react and react-dom to matching major versions",
        "fix_type": "safe_auto_apply",
        "confidence": 0.95,
        "is_flaky_test": False,
        "files_changed": [
            {
                "path": "package.json",
                "new_content": json.dumps(manifest),
                "explanation": "Bump react and react-dom to v18",
            }
        ],
        "category": "dependency",
        "logs_truncated_warning": False,
    }


@pytest.mark.asyncio
async def test_repo_memory_calibration_and_dependency_guardrail_compose(monkeypatch):
    captured = {}

    async def fake_call(**kwargs):
        captured["user_prompt"] = kwargs["user_prompt"]
        return _partial_peer_bump_raw_args()

    monkeypatch.setattr("app.agent.diagnosis_agent.call_with_tool", fake_call)

    diagnosis = await diagnose_failure(
        logs="npm ERR! peer react@'^18.0.0' from react-dom@18.2.0",
        repo_full_name="test/repo",
        commit_message="bump react",
        workflow_name="CI",
        model="unit",
        repo_memory=_poor_track_record_repo_memory(),
    )

    # M1: repo memory actually reached the prompt.
    assert "REPO MEMORY" in captured["user_prompt"]
    assert "dependency" in captured["user_prompt"]

    # M3: confidence capped by the poor effective track record (3 verified - 3 reverted = 0 -> ceiling 0.2),
    # which also pushes it through the static <0.4 gate as a speculative review.
    assert diagnosis.confidence <= 0.2
    assert diagnosis.speculative is True

    # M4: the partial peer bump (@types/react left on ^17) is caught independently
    # of the confidence path and explained in the fix description.
    assert diagnosis.fix_type == "review_recommended"
    assert "react@^18.2.0 vs @types/react@^17.0.39" in diagnosis.fix_description


@pytest.mark.asyncio
async def test_diagnose_failure_without_repo_memory_is_unaffected(monkeypatch):
    """Backward compatibility: the three call sites that don't build repo memory yet
    (deploy_repair.py, push_handler.py, force-fix) must keep working unchanged."""

    async def fake_call(**kwargs):
        return _partial_peer_bump_raw_args()

    monkeypatch.setattr("app.agent.diagnosis_agent.call_with_tool", fake_call)

    diagnosis = await diagnose_failure(
        logs="npm ERR! peer react@'^18.0.0' from react-dom@18.2.0",
        repo_full_name="test/repo",
        commit_message="bump react",
        workflow_name="CI",
        model="unit",
    )

    # M4 still fires with no repo_memory at all — it's independent of M1/M3.
    assert diagnosis.fix_type == "review_recommended"
    # M3 never touches confidence when there's no repo_memory to calibrate against.
    assert diagnosis.confidence == 0.95
