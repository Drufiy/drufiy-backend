import io
import os
import zipfile

import pytest

from app.agent.log_fetcher import (
    MAX_LOG_CHARS,
    PER_JOB_CHAR_BUDGET,
    LogsNotAvailableError,
    _parse_zip_logs,
    fetch_workflow_logs,
)


def _make_zip(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


def _padding(chars: int, line: str = "2026-07-30T00:00:00Z [PASS] noise line filling space\n") -> str:
    reps = chars // len(line) + 1
    return (line * reps)[:chars]


@pytest.mark.asyncio
async def test_fetch_404():
    """Fetching a non-existent run ID should raise LogsNotAvailableError."""
    token = os.getenv("TEST_GITHUB_TOKEN", "")
    if not token:
        pytest.skip("TEST_GITHUB_TOKEN not set")

    with pytest.raises(LogsNotAvailableError):
        await fetch_workflow_logs(
            github_run_id=9999999999,
            repo_full_name=os.getenv("TEST_REPO", "your-username/drufiy-test"),
            access_token=token,
        )


@pytest.mark.asyncio
async def test_fetch_real_logs():
    """Fetch real logs from a known run. Set env vars to run this test."""
    token = os.getenv("TEST_GITHUB_TOKEN", "")
    run_id = os.getenv("TEST_RUN_ID", "")
    repo = os.getenv("TEST_REPO", "")
    if not all([token, run_id, repo]):
        pytest.skip("TEST_GITHUB_TOKEN / TEST_RUN_ID / TEST_REPO not set")

    logs = await fetch_workflow_logs(
        github_run_id=int(run_id),
        repo_full_name=repo,
        access_token=token,
    )
    assert len(logs) > 0
    assert len(logs) <= 80_100


# M1: log-truncation bug fixtures — see ROADMAP.md "P1 BUG: Failure-blind log
# truncation". _parse_zip_logs() keeps only the LAST 80K chars of the whole
# concatenated blob. The two RED tests below reproduce the live PMSS and
# AgentCore failures: the real failure line gets discarded because it isn't
# in the tail. The two CONTROL tests confirm the cases that already work
# today keep working once M2-M4 land.

FAIL_MARKER = "FAIL_MARKER_UNIQUE: the actual failure lives here"


def test_single_huge_job_failure_at_top_is_lost():
    """RED — reproduces PMSS: one job's own log exceeds 80K chars, the real
    failure sits near the start (line 284 of 4510 in the live case), and
    thousands of trailing PASS lines push it out of the kept tail window."""
    content = FAIL_MARKER + "\n" + _padding(120_000)
    zip_bytes = _make_zip({"0_build.txt": content})

    result = _parse_zip_logs(zip_bytes)

    assert FAIL_MARKER in result, (
        "Failure at the start of a large single-job log was discarded by tail-only truncation"
    )


def test_single_huge_job_failure_at_bottom_survives():
    """CONTROL — the case tail-truncation already handles correctly. Must
    keep passing after the M2-M4 fix; proves the fix doesn't regress this."""
    content = _padding(120_000) + FAIL_MARKER
    zip_bytes = _make_zip({"0_build.txt": content})

    result = _parse_zip_logs(zip_bytes)

    assert FAIL_MARKER in result


def test_multi_job_failing_job_sorts_early_survives_even_without_failure_info():
    """GREEN as of M3 — originally written expecting this to stay RED forever
    (the degraded-mode fallback when _fetch_failing_job_names() can't
    determine which job failed). It turned green as a side effect of M3:
    _preprocess_logs() now runs on every section before truncation, and a
    section with no error-matching lines gets shrunk to its last 20 lines
    regardless of job order — so a huge passing job stops crowding out a
    small failing one even without M2's job-name-based reordering. Kept as a
    regression test for that degraded path, not because it's expected to
    fail; M2's reordering (see the "_survives_with_failure_info" test below)
    is still what production actually relies on when job info is available."""
    zip_bytes = _make_zip({
        "Backend (lint + test)/1_run.txt": FAIL_MARKER,
        "Mobile (typecheck)/1_run.txt": _padding(120_000, "2026-07-30T00:00:00Z [PASS] mobile step ok\n"),
    })

    result = _parse_zip_logs(zip_bytes)

    assert FAIL_MARKER in result, (
        "Failing job's log was excluded even though _preprocess_logs should have "
        "shrunk the noisy passing job enough to prevent this"
    )


def test_multi_job_failing_job_sorts_early_survives_with_failure_info():
    """GREEN once M2 lands — same shape as the test above, but this time the
    caller supplies which job actually failed (exactly what fetch_workflow_logs
    now does via _fetch_failing_job_names). The failing job's files should be
    reordered to survive truncation regardless of alphabetical position."""
    zip_bytes = _make_zip({
        "Backend (lint + test)/1_run.txt": FAIL_MARKER,
        "Mobile (typecheck)/1_run.txt": _padding(120_000, "2026-07-30T00:00:00Z [PASS] mobile step ok\n"),
    })

    result = _parse_zip_logs(zip_bytes, failing_job_names={"Backend (lint + test)"})

    assert FAIL_MARKER in result, (
        "Failing job's log was still excluded even though the caller identified it as failing"
    )


def test_multi_job_failing_job_sorts_late_survives():
    """CONTROL — the failing job sorts last, so today's tail-keep happens to
    include it by accident. Must keep passing after the fix."""
    zip_bytes = _make_zip({
        "Admin (typecheck)/1_run.txt": _padding(120_000, "2026-07-30T00:00:00Z [PASS] admin step ok\n"),
        "Frontend (lint + test)/1_run.txt": FAIL_MARKER,
    })

    result = _parse_zip_logs(zip_bytes)

    assert FAIL_MARKER in result


def _dense_errors(marker: str, lines: int = 1300) -> str:
    """Every line matches _ERROR_RE, so _filter_section_lines keeps ~all of
    it — simulates a job with substantial *genuine* error output (~47K chars),
    not noise _filter_section_lines would shrink away on its own."""
    body = "\n".join(f"error: step {i} failed unexpectedly" for i in range(lines))
    return body + f"\n{marker}"


def test_multi_job_dense_failures_dont_crowd_out_earlier_ones():
    """M4 — narrower residual case M2+M3 don't fully cover: 3 jobs each with
    substantial *genuine* error content (~47K chars each, ~141K combined,
    well over MAX_LOG_CHARS=80K). Without a per-job cap, the outer tail-cut
    keeps only the last 80K of the combined blob, which lands entirely
    inside Job B and C's sections — Job A's content, and specifically its
    identifying marker, is silently dropped in full, not just trimmed.
    Verified empirically before writing this test: removing the per-job cap
    in _preprocess_logs reproduces exactly that (JOBA_END_MARKER missing,
    B and C present)."""
    zip_bytes = _make_zip({
        "JobA.txt": _dense_errors("JOBA_END_MARKER"),
        "JobB.txt": _dense_errors("JOBB_END_MARKER"),
        "JobC.txt": _dense_errors("JOBC_END_MARKER"),
    })
    # Sanity: each job's raw content alone exceeds the per-job budget, and
    # the combined total exceeds MAX_LOG_CHARS — the scenario this defends.
    assert len(_dense_errors("x")) > PER_JOB_CHAR_BUDGET
    assert 3 * len(_dense_errors("x")) > MAX_LOG_CHARS

    result = _parse_zip_logs(zip_bytes)

    assert "JOBA_END_MARKER" in result, "Job A's error content was dropped entirely, not just trimmed"
    assert "JOBB_END_MARKER" in result, "Job B's error content was dropped entirely, not just trimmed"
    assert "JOBC_END_MARKER" in result, "Job C's error content was dropped entirely, not just trimmed"
