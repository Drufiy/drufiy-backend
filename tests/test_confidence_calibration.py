"""
M3: confidence recalibration against historical per-repo/category outcomes.
Pure unit tests — no API calls, no Supabase.
"""
from app.agent.diagnosis_agent import MIN_CALIBRATION_SAMPLES, _recalibrate_confidence
from app.agent.repo_memory import RepoMemory
from app.agent.schemas import Diagnosis, FileChange


def _diagnosis(confidence: float, fix_type: str = "safe_auto_apply", category: str = "dependency") -> Diagnosis:
    return Diagnosis(
        problem_summary="Peer dependency conflict in package.json",
        root_cause="react-dom pinned to an incompatible version of react",
        fix_description="Bump react-dom to match react's major version",
        fix_type=fix_type,
        confidence=confidence,
        category=category,
        files_changed=[
            FileChange(path="package.json", new_content="{}", explanation="bump version")
        ],
    )


def _memory(attempts: int, verified: int, reverted: int = 0, category: str = "dependency") -> RepoMemory:
    memory = RepoMemory(repo_id="repo-1")
    memory.category_outcomes = {
        category: {
            "attempts": attempts,
            "verified": verified,
            "failed": 0,
            "rejected": 0,
            "reverted": reverted,
            "exhausted": 0,
            "verified_rate": round(verified / attempts, 2) if attempts else None,
        }
    }
    return memory


def test_caps_confidence_with_poor_track_record():
    # 1/5 verified -> effective rate 0.2 -> ceiling 0.4
    diagnosis = _diagnosis(confidence=0.95)
    memory = _memory(attempts=5, verified=1)

    result = _recalibrate_confidence(diagnosis, memory, run_id="run-1")

    assert result.confidence == 0.4
    assert result.fix_type == "safe_auto_apply"  # gating happens in the static checks downstream


def test_skips_below_minimum_sample_size():
    assert MIN_CALIBRATION_SAMPLES == 4
    diagnosis = _diagnosis(confidence=0.95)
    memory = _memory(attempts=2, verified=0)  # too few samples to trust

    result = _recalibrate_confidence(diagnosis, memory, run_id="run-1")

    assert result.confidence == 0.95


def test_reverted_fixes_count_against_the_rate():
    # Naive verified_rate = 4/5 = 0.8 (would not trigger a cap), but 3 of those
    # 4 "verified" fixes were reverted within 7 days -> effective rate is actually 0.2.
    diagnosis = _diagnosis(confidence=0.9)
    memory = _memory(attempts=5, verified=4, reverted=3)

    result = _recalibrate_confidence(diagnosis, memory, run_id="run-1")

    assert result.confidence == 0.4


def test_good_track_record_leaves_confidence_untouched():
    diagnosis = _diagnosis(confidence=0.9)
    memory = _memory(attempts=5, verified=5, reverted=0)

    result = _recalibrate_confidence(diagnosis, memory, run_id="run-1")

    assert result.confidence == 0.9


def test_no_repo_memory_is_a_no_op():
    diagnosis = _diagnosis(confidence=0.95)

    result = _recalibrate_confidence(diagnosis, None, run_id="run-1")

    assert result.confidence == 0.95


def test_category_with_no_history_is_a_no_op():
    diagnosis = _diagnosis(confidence=0.95, category="dependency")
    memory = _memory(attempts=10, verified=1, category="workflow_config")  # different category

    result = _recalibrate_confidence(diagnosis, memory, run_id="run-1")

    assert result.confidence == 0.95
