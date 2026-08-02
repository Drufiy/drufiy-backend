"""
M9 (B4): a manual_required code diagnosis is a dead end for the user — no
PR, nothing to review. Retry once with force_fix to get a speculative
best-guess PR instead. No API calls — the model call is faked, same
pattern as test_diagnosis_guardrails.py.
"""
import pytest

from app.agent.diagnosis_agent import diagnose_failure

MANUAL_REQUIRED_CODE = {
    "problem_summary": "A code failure the model couldn't confidently resolve",
    "root_cause": "The root cause is unclear from the available logs and context",
    "fix_description": "Unable to determine a safe fix from the available information",
    "fix_type": "manual_required",
    "confidence": 0.5,
    "is_flaky_test": False,
    "files_changed": [],
    "category": "code",
    "logs_truncated_warning": False,
}

MANUAL_REQUIRED_ENVIRONMENT = {
    "problem_summary": "Deploy fails because a required secret is missing",
    "root_cause": "STRIPE_SECRET_KEY is not defined in the GitHub Actions environment",
    "fix_description": "Add the missing secret in repo settings",
    "fix_type": "manual_required",
    "confidence": 0.9,
    "is_flaky_test": False,
    "files_changed": [],
    "category": "environment",
    "logs_truncated_warning": False,
}

FORCED_BEST_GUESS = {
    "problem_summary": "A code failure the model couldn't confidently resolve",
    "root_cause": "The root cause is unclear from the available logs and context",
    "fix_description": "Best-guess speculative fix attempt",
    "fix_type": "review_recommended",
    "confidence": 0.55,
    "is_flaky_test": False,
    "files_changed": [
        {"path": "src/app.py", "new_content": "# best guess fix", "explanation": "speculative attempt"}
    ],
    "category": "code",
    "logs_truncated_warning": False,
}


@pytest.mark.asyncio
async def test_manual_required_code_retries_and_becomes_speculative(monkeypatch):
    call_count = 0

    async def fake_call(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return dict(MANUAL_REQUIRED_CODE)
        # Second call is the force_fix retry — assert the override instruction
        # actually reached the model.
        assert "USER OVERRIDE" in kwargs["user_prompt"]
        return dict(FORCED_BEST_GUESS)

    monkeypatch.setattr("app.agent.diagnosis_agent.call_with_tool", fake_call)
    diagnosis = await diagnose_failure(
        logs="Error: something failed in a way that's hard to diagnose",
        repo_full_name="test/repo",
        commit_message="test",
        workflow_name="CI",
        model="unit",
    )

    assert call_count == 2
    assert diagnosis.fix_type == "review_recommended"
    assert diagnosis.speculative is True
    assert diagnosis.files_changed


@pytest.mark.asyncio
async def test_manual_required_environment_does_not_retry(monkeypatch):
    call_count = 0

    async def fake_call(**kwargs):
        nonlocal call_count
        call_count += 1
        return dict(MANUAL_REQUIRED_ENVIRONMENT)

    monkeypatch.setattr("app.agent.diagnosis_agent.call_with_tool", fake_call)
    diagnosis = await diagnose_failure(
        logs="Error: STRIPE_SECRET_KEY is not defined",
        repo_full_name="test/repo",
        commit_message="test",
        workflow_name="CI",
        model="unit",
    )

    assert call_count == 1  # no retry — environment genuinely can't be guessed at
    assert diagnosis.fix_type == "manual_required"
    assert diagnosis.category == "environment"


@pytest.mark.asyncio
async def test_already_forced_does_not_retry_again(monkeypatch):
    call_count = 0

    async def fake_call(**kwargs):
        nonlocal call_count
        call_count += 1
        return dict(MANUAL_REQUIRED_CODE)

    monkeypatch.setattr("app.agent.diagnosis_agent.call_with_tool", fake_call)
    diagnosis = await diagnose_failure(
        logs="Error: something failed in a way that's hard to diagnose",
        repo_full_name="test/repo",
        commit_message="test",
        workflow_name="CI",
        model="unit",
        force_fix=True,  # already a retry attempt — must not recurse further
    )

    assert call_count == 1  # bounded to exactly one attempt, no infinite recursion
    assert diagnosis.fix_type == "manual_required"


@pytest.mark.asyncio
async def test_retry_still_fails_keeps_original_diagnosis(monkeypatch):
    call_count = 0

    async def fake_call(**kwargs):
        nonlocal call_count
        call_count += 1
        # Even the forced retry can't produce anything — genuinely nothing to guess at.
        return dict(MANUAL_REQUIRED_CODE)

    monkeypatch.setattr("app.agent.diagnosis_agent.call_with_tool", fake_call)
    diagnosis = await diagnose_failure(
        logs="Error: something failed in a way that's hard to diagnose",
        repo_full_name="test/repo",
        commit_message="test",
        workflow_name="CI",
        model="unit",
    )

    assert call_count == 2  # the retry was attempted
    assert diagnosis.fix_type == "manual_required"  # but genuinely gave up both times
    assert diagnosis.speculative is False


@pytest.mark.asyncio
async def test_successful_diagnosis_never_triggers_retry(monkeypatch):
    call_count = 0

    async def fake_call(**kwargs):
        nonlocal call_count
        call_count += 1
        return {
            "problem_summary": "Missing dependency",
            "root_cause": "Package not installed",
            "fix_description": "Add the missing package",
            "fix_type": "safe_auto_apply",
            "confidence": 0.95,
            "is_flaky_test": False,
            "files_changed": [{"path": "package.json", "new_content": "{}", "explanation": "add dep"}],
            "category": "dependency",
            "logs_truncated_warning": False,
        }

    monkeypatch.setattr("app.agent.diagnosis_agent.call_with_tool", fake_call)
    diagnosis = await diagnose_failure(
        logs="npm ERR! missing dependency",
        repo_full_name="test/repo",
        commit_message="test",
        workflow_name="CI",
        model="unit",
    )

    assert call_count == 1
    assert diagnosis.fix_type == "safe_auto_apply"
