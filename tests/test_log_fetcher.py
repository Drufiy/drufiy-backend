import os

import pytest

from app.agent.log_fetcher import LogsNotAvailableError, fetch_workflow_logs


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
