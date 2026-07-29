"""
M4: dependency chain completeness — catches a partial peer/type package bump
(e.g. react bumped, @types/react left behind) before it ships as safe_auto_apply.
Pure unit tests — no API calls, no Supabase.
"""
import json

from app.agent.diagnosis_agent import _check_dependency_chain_completeness, _extract_major_version
from app.agent.schemas import Diagnosis, FileChange


def _package_json_diagnosis(manifest: dict, fix_type: str = "safe_auto_apply") -> Diagnosis:
    return Diagnosis(
        problem_summary="React peer dependency conflict",
        root_cause="react-dom pinned to an incompatible version of react",
        fix_description="Bump react-dom to match react's major version",
        fix_type=fix_type,
        confidence=0.9,
        category="dependency",
        files_changed=[
            FileChange(path="package.json", new_content=json.dumps(manifest), explanation="bump versions")
        ],
    )


def test_extract_major_version():
    assert _extract_major_version("^18.2.0") == 18
    assert _extract_major_version("~5.0.1") == 5
    assert _extract_major_version("18.2.0") == 18
    assert _extract_major_version(">=4.0.0 <5.0.0") == 4
    assert _extract_major_version("*") is None
    assert _extract_major_version("latest") is None
    assert _extract_major_version("workspace:*") is None
    assert _extract_major_version(None) is None


def test_flags_partial_bump_react_types_mismatch():
    # react bumped to 18, but @types/react left on 17 — the exact lagom-humanizer-style gap.
    diagnosis = _package_json_diagnosis({
        "dependencies": {"react": "^18.2.0", "react-dom": "^18.2.0"},
        "devDependencies": {"@types/react": "^17.0.39", "@types/react-dom": "^18.2.0"},
    })

    result = _check_dependency_chain_completeness(diagnosis)

    assert result.fix_type == "review_recommended"
    assert result.speculative is True
    assert "react@^18.2.0 vs @types/react@^17.0.39" in result.fix_description


def test_complete_bump_is_untouched():
    # All four packages bumped together — no mismatch, stays safe_auto_apply.
    diagnosis = _package_json_diagnosis({
        "dependencies": {"react": "^18.2.0", "react-dom": "^18.2.0"},
        "devDependencies": {"@types/react": "^18.2.0", "@types/react-dom": "^18.2.0"},
    })

    result = _check_dependency_chain_completeness(diagnosis)

    assert result.fix_type == "safe_auto_apply"
    assert result.speculative is False
    assert result is diagnosis


def test_no_package_json_is_a_no_op():
    diagnosis = Diagnosis(
        problem_summary="Missing import in requirements.txt",
        root_cause="requests package not declared",
        fix_description="Add requests to requirements.txt",
        fix_type="safe_auto_apply",
        confidence=0.9,
        category="dependency",
        files_changed=[
            FileChange(path="requirements.txt", new_content="requests==2.31.0\n", explanation="add requests")
        ],
    )

    result = _check_dependency_chain_completeness(diagnosis)

    assert result is diagnosis


def test_malformed_package_json_does_not_crash():
    diagnosis = Diagnosis(
        problem_summary="React peer dependency conflict",
        root_cause="react-dom pinned to an incompatible version of react",
        fix_description="Bump react-dom to match react's major version",
        fix_type="safe_auto_apply",
        confidence=0.9,
        category="dependency",
        files_changed=[
            FileChange(path="package.json", new_content="{not valid json", explanation="bump versions")
        ],
    )

    result = _check_dependency_chain_completeness(diagnosis)

    assert result is diagnosis


def test_unpaired_package_is_ignored():
    # Only react bumped, react-dom not present in the manifest at all (e.g. server-only
    # package) — nothing to compare against, so no false positive.
    diagnosis = _package_json_diagnosis({"dependencies": {"react": "^18.2.0"}})

    result = _check_dependency_chain_completeness(diagnosis)

    assert result.fix_type == "safe_auto_apply"
