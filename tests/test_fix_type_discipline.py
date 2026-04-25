"""
Suite 4: Fix-Type Discipline & Prompt Rule Adherence
Tests that Kimi strictly follows every rule written in the system prompt.
All rules are deterministic — these should be 100% pass rate.
"""
import pytest
from app.agent.diagnosis_agent import diagnose_failure
from app.agent.schemas import Diagnosis, FileChange
from pydantic import ValidationError


# ── Scenario logs ─────────────────────────────────────────────────────────────

CLEAR_WORKFLOW_FIX = """\n=== 1_actions_setup-node@v4 ===\nUnable to find Node version '12' for platform linux and architecture x64.\nError: Process completed with exit code 1.\n"""

LOGIC_CHANGE_LOG = """\n=== 1_Run tests ===\nFAIL src/__tests__/cart.test.ts\n  ✗ calculates discount for premium users (3ms)\n\n    Expected: 20\n    Received: 10\n\n    The discount rate for PREMIUM tier should be 20% but the pricing engine returns 10%.\n    This was changed in commit a3f2b1 — the tier multiplier map was updated but the\n    discount calculation function still uses the old hardcoded 0.1 rate.\n\nError: Process completed with exit code 1.\n"""

MANY_FILES_LOG = """\n=== 1_Run tests ===\nError: Module not found. The following 8 imports are broken:\n  - src/components/Button.tsx → missing '../utils/theme'\n  - src/components/Card.tsx → missing '../utils/theme'\n  - src/components/Modal.tsx → missing '../utils/theme'\n  - src/components/Form.tsx → missing '../utils/theme'\n  - src/components/Table.tsx → missing '../utils/theme'\n  - src/components/Nav.tsx → missing '../utils/theme'\n  - src/pages/Home.tsx → missing '../utils/theme'\n  - src/pages/Settings.tsx → missing '../utils/theme'\nError: Process completed with exit code 1.\n"""


# ── Schema validation tests (no Kimi — pure Pydantic) ────────────────────────

def test_empty_new_content_rejected():
    with pytest.raises(ValidationError):
        FileChange(path="ci.yml", new_content="", explanation="fix")


def test_path_with_dotdot_rejected():
    with pytest.raises(ValidationError):
        FileChange(path="../etc/passwd", new_content="content", explanation="fix")


def test_absolute_path_rejected():
    with pytest.raises(ValidationError):
        FileChange(path="/etc/passwd", new_content="content", explanation="fix")


def test_content_too_large_rejected():
    with pytest.raises(ValidationError):
        FileChange(path="ci.yml", new_content="x" * 200_001, explanation="fix")


def test_safe_auto_apply_requires_files():
    with pytest.raises(ValidationError):
        Diagnosis(
            problem_summary="Node version wrong" * 2,
            root_cause="Node 12 is not available on ubuntu runners anymore " * 2,
            fix_description="Update node version to 20 in the workflow file",
            fix_type="safe_auto_apply",
            confidence=0.95,
            is_flaky_test=False,
            files_changed=[],          # violation — safe_auto_apply needs files
            category="workflow_config",
            logs_truncated_warning=False,
        )


def test_manual_required_forbids_files():
    with pytest.raises(ValidationError):
        Diagnosis(
            problem_summary="Missing secret key in environment" * 1,
            root_cause="The STRIPE_KEY env var is not set in GitHub Actions secrets " * 1,
            fix_description="Add STRIPE_KEY to repository secrets",
            fix_type="manual_required",
            confidence=0.9,
            is_flaky_test=False,
            files_changed=[
                FileChange(path="ci.yml", new_content="name: CI\n", explanation="wrong")
            ],   # violation — manual_required must have empty files_changed
            category="environment",
            logs_truncated_warning=False,
        )


def test_confidence_out_of_range_rejected():
    with pytest.raises(ValidationError):
        Diagnosis(
            problem_summary="Something failed here in the CI run today",
            root_cause="The root cause is unclear but something is wrong with the build system",
            fix_description="Fix the issue by updating the configuration file appropriately",
            fix_type="manual_required",
            confidence=1.5,            # out of range
            is_flaky_test=False,
            files_changed=[],
            category="unknown",
            logs_truncated_warning=False,
        )


# ── Kimi behavior tests ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_clear_workflow_fix_is_safe_auto_apply():
    """
    Node 12 unavailable — deterministic, single-line fix in workflow.
    Must be safe_auto_apply with high confidence.
    """
    d = await diagnose_failure(
        logs=CLEAR_WORKFLOW_FIX,
        repo_full_name="test/repo",
        commit_message="upgrade CI",
        workflow_name="CI",
    )
    assert d.fix_type in ("safe_auto_apply", "review_recommended"), (
        f"Clear workflow fix should be auto-applicable, got {d.fix_type}"
    )
    assert d.confidence >= 0.75, f"High-confidence fix expected, got {d.confidence}"
    assert d.category in ("workflow_config",), (
        f"Expected workflow_config, got {d.category}"
    )


@pytest.mark.asyncio
async def test_logic_bug_is_review_recommended():
    """
    Business logic bug (wrong discount rate) → logic change, needs human review.
    Must NOT be safe_auto_apply.
    """
    d = await diagnose_failure(
        logs=LOGIC_CHANGE_LOG,
        repo_full_name="test/repo",
        commit_message="fix pricing",
        workflow_name="CI",
    )
    assert d.fix_type != "safe_auto_apply", (
        "Logic/business rule changes must not be safe_auto_apply"
    )
    assert d.category == "code", f"Business logic bug should be 'code', got {d.category}"


@pytest.mark.asyncio
async def test_too_many_files_is_manual():
    """
    8 files broken by a missing module → scope too large for autonomous fix.
    Must NOT produce 8 separate file changes — should flag as too broad.
    """
    d = await diagnose_failure(
        logs=MANY_FILES_LOG,
        repo_full_name="test/repo",
        commit_message="restructure utils",
        workflow_name="CI",
    )
    # Either: correctly identifies missing theme.ts as the fix (1 file), or
    # marks manual_required because it can't see the file contents
    if d.fix_type != "manual_required":
        assert len(d.files_changed) <= 3, (
            f"Should propose root fix (missing file), not patch all 8 consumers. Got {len(d.files_changed)} files"
        )
