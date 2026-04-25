"""
Suite 7: Pydantic Schema Validation (pure unit tests — no Kimi calls)
Validates that our schema guards catch every class of bad model output
before it reaches the database or GitHub API.
Fast — runs in < 1 second.
"""
import pytest
from pydantic import ValidationError
from app.agent.schemas import Diagnosis, FileChange


# ── FileChange validation ─────────────────────────────────────────────────────

def test_valid_file_change():
    fc = FileChange(
        path=".github/workflows/ci.yml",
        new_content="name: CI\non: [push]\njobs:\n  test:\n    runs-on: ubuntu-latest\n",
        explanation="Updated node version to 20",
    )
    assert fc.path == ".github/workflows/ci.yml"


def test_path_absolute_rejected():
    with pytest.raises(ValidationError, match="relative"):
        FileChange(path="/etc/passwd", new_content="root:x:0:0", explanation="bad")


def test_path_traversal_rejected():
    with pytest.raises(ValidationError, match="\\.\\."):
        FileChange(path="../../etc/shadow", new_content="hack", explanation="bad")


def test_empty_content_rejected():
    with pytest.raises(ValidationError):
        FileChange(path="ci.yml", new_content="   ", explanation="empty")


def test_content_200kb_limit():
    with pytest.raises(ValidationError, match="200KB"):
        FileChange(path="ci.yml", new_content="x" * 200_001, explanation="too big")


def test_content_exactly_200kb_allowed():
    fc = FileChange(path="ci.yml", new_content="x" * 200_000, explanation="at limit")
    assert len(fc.new_content) == 200_000


# ── Diagnosis validation ──────────────────────────────────────────────────────

def _valid_diag(**overrides) -> dict:
    base = dict(
        problem_summary="npm install fails due to missing jsonwebtoken package",
        root_cause="The package jsonwebtoken is used in src/auth.ts but not listed in package.json dependencies. It was likely a transitive dependency that got removed in a recent update.",
        fix_description="Add jsonwebtoken to package.json dependencies section",
        fix_type="safe_auto_apply",
        confidence=0.92,
        is_flaky_test=False,
        files_changed=[FileChange(
            path="package.json",
            new_content='{"dependencies": {"jsonwebtoken": "^9.0.0"}}',
            explanation="Added jsonwebtoken dependency",
        )],
        category="dependency",
        logs_truncated_warning=False,
    )
    base.update(overrides)
    return base


def test_valid_diagnosis_parses():
    d = Diagnosis(**_valid_diag())
    assert d.fix_type == "safe_auto_apply"
    assert d.confidence == 0.92


def test_confidence_above_1_rejected():
    with pytest.raises(ValidationError):
        Diagnosis(**_valid_diag(confidence=1.1))


def test_confidence_below_0_rejected():
    with pytest.raises(ValidationError):
        Diagnosis(**_valid_diag(confidence=-0.1))


def test_invalid_fix_type_rejected():
    with pytest.raises(ValidationError):
        Diagnosis(**_valid_diag(fix_type="auto_yolo"))


def test_invalid_category_rejected():
    with pytest.raises(ValidationError):
        Diagnosis(**_valid_diag(category="vibes"))


def test_problem_summary_too_short_rejected():
    with pytest.raises(ValidationError):
        Diagnosis(**_valid_diag(problem_summary="bad"))


def test_root_cause_too_short_rejected():
    with pytest.raises(ValidationError):
        Diagnosis(**_valid_diag(root_cause="short"))


def test_safe_auto_apply_with_no_files_rejected():
    with pytest.raises(ValidationError):
        Diagnosis(**_valid_diag(fix_type="safe_auto_apply", files_changed=[]))


def test_manual_required_with_files_rejected():
    with pytest.raises(ValidationError):
        Diagnosis(**_valid_diag(
            fix_type="manual_required",
            files_changed=[FileChange(
                path="package.json",
                new_content='{"a": 1}',
                explanation="should not be here",
            )],
        ))


def test_review_recommended_with_no_files_rejected():
    with pytest.raises(ValidationError):
        Diagnosis(**_valid_diag(fix_type="review_recommended", files_changed=[]))


def test_flaky_test_flag_no_business_logic_enforced_by_schema():
    """Schema doesn't enforce is_flaky_test → manual_required. Agent layer does."""
    # This should pass schema validation (agent post-validates separately)
    d = Diagnosis(**_valid_diag(is_flaky_test=True))
    assert d.is_flaky_test is True


def test_problem_summary_max_length():
    with pytest.raises(ValidationError):
        Diagnosis(**_valid_diag(problem_summary="x" * 501))


def test_multiple_files_allowed():
    d = Diagnosis(**_valid_diag(
        fix_type="review_recommended",
        files_changed=[
            FileChange(path="package.json", new_content='{"a":1}', explanation="one"),
            FileChange(path=".github/workflows/ci.yml", new_content="name: CI\n", explanation="two"),
        ],
    ))
    assert len(d.files_changed) == 2
