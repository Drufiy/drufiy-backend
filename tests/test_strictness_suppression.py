"""
M6: diagnosis quality — flag when a fix loosens an analyzer/linter/test gate
instead of resolving what it caught, per ROADMAP.md's "Diagnosis quality:
root-cause fixes over strictness suppression" finding (found live on
rpcs3-compatibility, 2026-07-30). Pure unit tests — no API calls.
"""
from app.agent.diagnosis_agent import _flag_strictness_suppression
from app.agent.schemas import Diagnosis, FileChange


def _diagnosis(fix_description: str, root_cause: str = "A real underlying issue caused this failure.") -> Diagnosis:
    return Diagnosis(
        problem_summary="PHPStan level 9 found 2 type errors in functions.php",
        root_cause=root_cause,
        fix_description=fix_description,
        fix_type="review_recommended",
        confidence=0.75,
        category="workflow_config",
        files_changed=[
            FileChange(path=".github/workflows/master.yml", new_content="level: 5", explanation="lower level")
        ],
    )


def test_real_captured_suppression_gets_flagged():
    # The exact fix_description text captured live from the rpcs3-compatibility
    # PR that prompted this milestone — verified this doesn't match a naive
    # "lowered the level" adjacent-phrase regex (PHPStan sits in between).
    real_text = (
        "Lower PHPStan analysis level from 9 to 5 to match typical legacy PHP project "
        "standards and prevent strict type errors from failing CI. This is a common and "
        "safe adjustment for older PHP codebases that were not written with level 9 "
        "strictness in mind."
    )
    diagnosis = _diagnosis(real_text)

    result = _flag_strictness_suppression(diagnosis)

    assert result.fix_description.lower().startswith("note:")
    assert "relax" in result.fix_description.lower() or "suppress" in result.fix_description.lower()
    assert real_text in result.fix_description  # original description preserved, not replaced


def test_normal_root_cause_fix_is_untouched():
    normal_text = (
        "Added an explicit string cast and type guard around the mixed value at line 42, "
        "satisfying PHPStan level 9 by resolving the actual type-safety gap it flagged."
    )
    diagnosis = _diagnosis(normal_text)

    result = _flag_strictness_suppression(diagnosis)

    assert result.fix_description == normal_text
    assert result is diagnosis


def test_already_honestly_disclosed_is_not_double_flagged():
    already_disclosed = (
        "Note: this relaxes the analyzer rather than fixing the underlying issue — lowered "
        "PHPStan from level 9 to 5 because the flagged type-safety gaps span many legacy "
        "files beyond the scope of this CI failure."
    )
    diagnosis = _diagnosis(already_disclosed)

    result = _flag_strictness_suppression(diagnosis)

    assert result.fix_description == already_disclosed
    assert result is diagnosis
    assert result.fix_description.lower().count("note:") == 1


def test_eslint_rule_disable_is_flagged():
    diagnosis = _diagnosis("Disabled the no-unused-vars ESLint rule to unblock the build.")

    result = _flag_strictness_suppression(diagnosis)

    assert result.fix_description.lower().startswith("note:")


def test_test_skip_language_is_flagged():
    diagnosis = _diagnosis("Skipped the flaky integration test so CI passes reliably.")

    result = _flag_strictness_suppression(diagnosis)

    assert result.fix_description.lower().startswith("note:")


def test_suppression_language_in_root_cause_alone_is_flagged():
    # The check scans fix_description + root_cause together — a model might
    # explain the suppression in root_cause while writing a bland fix_description.
    diagnosis = _diagnosis(
        fix_description="Updated the CI workflow configuration.",
        root_cause="The team decided to lower the strictness level to unblock releases.",
    )

    result = _flag_strictness_suppression(diagnosis)

    assert result.fix_description.lower().startswith("note:")
