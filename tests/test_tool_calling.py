"""
Suite 2: Tool Calling Compliance
Kimi MUST call submit_diagnosis on every invocation — never plain text.
"""
import pytest
from app.agent.diagnosis_agent import diagnose_failure
from app.agent.schemas import Diagnosis


REAL_LOG = """\n=== 1_Run npm test ===\nnpm ERR! code ERESOLVE\nnpm ERR! Could not resolve dependency:\nnpm ERR! peer react@"^17.0.0" from react-dom@17.0.2\nnpm ERR! Found: react@18.2.0\nError: Process completed with exit code 1.\n"""

GIBBERISH_LOG = "asjdh 923 !@#$ ??? %%%% ~~~~~ undefined null NaN"

MINIMAL_LOG = "Error: exit code 1"


@pytest.mark.asyncio
async def test_tool_called_not_prose():
    """Kimi must return a Diagnosis object, never a plain-text string."""
    result = await diagnose_failure(
        logs=REAL_LOG,
        repo_full_name="test/repo",
        commit_message="update deps",
        workflow_name="CI",
    )
    assert isinstance(result, Diagnosis), (
        f"Expected Diagnosis object, got {type(result)}"
    )


@pytest.mark.asyncio
async def test_all_required_fields_populated():
    """Every required field must be present and non-empty."""
    d = await diagnose_failure(
        logs=REAL_LOG,
        repo_full_name="test/repo",
        commit_message="update deps",
        workflow_name="CI",
    )
    assert d.problem_summary and len(d.problem_summary) >= 10
    assert d.root_cause and len(d.root_cause) >= 20
    assert d.fix_description and len(d.fix_description) >= 20
    assert d.fix_type in ("safe_auto_apply", "review_recommended", "manual_required")
    assert 0.0 <= d.confidence <= 1.0
    assert isinstance(d.is_flaky_test, bool)
    assert d.category in ("code", "workflow_config", "dependency", "environment", "flaky_test", "unknown")
    assert isinstance(d.logs_truncated_warning, bool)


@pytest.mark.asyncio
async def test_gibberish_input_still_calls_tool():
    """
    Even with nonsense input, Kimi must call the tool — not return prose.
    Should produce manual_required with low confidence.
    """
    d = await diagnose_failure(
        logs=GIBBERISH_LOG,
        repo_full_name="test/repo",
        commit_message="???",
        workflow_name="CI",
    )
    assert isinstance(d, Diagnosis)
    assert d.fix_type == "manual_required", (
        f"Gibberish logs must yield manual_required, got {d.fix_type}"
    )
    assert d.confidence < 0.6, (
        f"Gibberish logs must yield low confidence, got {d.confidence}"
    )


@pytest.mark.asyncio
async def test_minimal_log_still_calls_tool():
    """Single-line error still produces a structured response."""
    d = await diagnose_failure(
        logs=MINIMAL_LOG,
        repo_full_name="test/repo",
        commit_message="fix",
        workflow_name="CI",
    )
    assert isinstance(d, Diagnosis)
    # Can't diagnose from one line — must be honest about it
    assert d.confidence < 0.7
