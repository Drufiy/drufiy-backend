"""
M8: multi-model consensus for low-confidence/unknown diagnoses. Pure unit
tests — the Kimi call is faked, same pattern as test_diagnosis_guardrails.py.
"""
import pytest

from app.agent.diagnosis_agent import _LOW_CONFIDENCE_THRESHOLD, _consult_second_opinion
from app.agent.schemas import Diagnosis, FileChange


def _diagnosis(confidence: float, fix_type: str = "review_recommended", category: str = "code") -> Diagnosis:
    return Diagnosis(
        problem_summary="Something failed in the build",
        root_cause="A root cause that isn't fully clear from the available logs",
        fix_description="A speculative fix attempt",
        fix_type=fix_type,
        confidence=confidence,
        category=category,
        files_changed=[FileChange(path="src/app.py", new_content="# fixed", explanation="attempted fix")],
    )


VALID_SECOND_ARGS = {
    "problem_summary": "Kimi's independent read of the same failure",
    "root_cause": "Kimi's independent root cause analysis of the failure",
    "fix_description": "Kimi's independently proposed fix for the same issue",
    "fix_type": "review_recommended",
    "confidence": 0.6,
    "category": "code",
    "files_changed": [],
    "is_flaky_test": False,
    "logs_truncated_warning": False,
}


@pytest.mark.asyncio
async def test_high_confidence_skips_second_opinion(monkeypatch):
    called = False

    async def fake_call(*args, **kwargs):
        nonlocal called
        called = True
        return VALID_SECOND_ARGS, "{}", {}

    monkeypatch.setattr("app.agent.diagnosis_agent._call_kimi_structured", fake_call)
    diagnosis = _diagnosis(confidence=0.9, category="code")

    result = await _consult_second_opinion(diagnosis, "system", "user", run_id="run-1")

    assert called is False
    assert result is diagnosis


@pytest.mark.asyncio
async def test_unknown_category_triggers_even_with_high_confidence(monkeypatch):
    async def fake_call(*args, **kwargs):
        return VALID_SECOND_ARGS, "{}", {}

    monkeypatch.setattr("app.agent.diagnosis_agent._call_kimi_structured", fake_call)
    # High confidence but unknown category — trigger is "OR", not "AND".
    diagnosis = _diagnosis(confidence=0.9, category="unknown")

    result = await _consult_second_opinion(diagnosis, "system", "user", run_id="run-1")

    assert "Cross-model check" in result.fix_description


@pytest.mark.asyncio
async def test_agreement_is_noted(monkeypatch):
    async def fake_call(*args, **kwargs):
        agreeing = dict(VALID_SECOND_ARGS)
        agreeing["fix_type"] = "review_recommended"
        agreeing["category"] = "code"
        return agreeing, "{}", {}

    monkeypatch.setattr("app.agent.diagnosis_agent._call_kimi_structured", fake_call)
    diagnosis = _diagnosis(confidence=0.3, fix_type="review_recommended", category="code")

    result = await _consult_second_opinion(diagnosis, "system", "user", run_id="run-1")

    assert "agrees with this diagnosis" in result.fix_description
    assert "DISAGREES" not in result.fix_description


@pytest.mark.asyncio
async def test_disagreement_is_flagged(monkeypatch):
    async def fake_call(*args, **kwargs):
        disagreeing = dict(VALID_SECOND_ARGS)
        disagreeing["fix_type"] = "manual_required"
        disagreeing["category"] = "environment"
        disagreeing["files_changed"] = []
        return disagreeing, "{}", {}

    monkeypatch.setattr("app.agent.diagnosis_agent._call_kimi_structured", fake_call)
    diagnosis = _diagnosis(confidence=0.3, fix_type="review_recommended", category="code")

    result = await _consult_second_opinion(diagnosis, "system", "user", run_id="run-1")

    assert "DISAGREES" in result.fix_description
    assert "manual_required" in result.fix_description
    # Original fix_type/confidence must NOT change — only the description is annotated.
    assert result.fix_type == "review_recommended"
    assert result.confidence == 0.3


@pytest.mark.asyncio
async def test_kimi_failure_does_not_crash_or_change_diagnosis(monkeypatch):
    async def fake_call(*args, **kwargs):
        raise RuntimeError("Kimi API is down")

    monkeypatch.setattr("app.agent.diagnosis_agent._call_kimi_structured", fake_call)
    diagnosis = _diagnosis(confidence=0.3)

    result = await _consult_second_opinion(diagnosis, "system", "user", run_id="run-1")

    assert result is diagnosis


@pytest.mark.asyncio
async def test_kimi_invalid_response_is_ignored(monkeypatch):
    async def fake_call(*args, **kwargs):
        # Missing required keys — shouldn't pass _args_match_schema.
        return {"problem_summary": "incomplete"}, "{}", {}

    monkeypatch.setattr("app.agent.diagnosis_agent._call_kimi_structured", fake_call)
    diagnosis = _diagnosis(confidence=0.3)

    result = await _consult_second_opinion(diagnosis, "system", "user", run_id="run-1")

    assert result is diagnosis


@pytest.mark.asyncio
async def test_threshold_boundary_is_exclusive(monkeypatch):
    called = False

    async def fake_call(*args, **kwargs):
        nonlocal called
        called = True
        return VALID_SECOND_ARGS, "{}", {}

    monkeypatch.setattr("app.agent.diagnosis_agent._call_kimi_structured", fake_call)
    # Exactly at the threshold should NOT trigger (>=  means already "confident enough").
    diagnosis = _diagnosis(confidence=_LOW_CONFIDENCE_THRESHOLD, category="code")

    await _consult_second_opinion(diagnosis, "system", "user", run_id="run-1")

    assert called is False
