"""
Diagnostic quality tests — run against real Kimi K2.6.
These assert that the model correctly classifies common CI failure patterns.
"""
import os
import pytest
from pathlib import Path

from app.agent.diagnosis_agent import diagnose_failure

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.asyncio
async def test_node_version_mismatch():
    """
    Node 14 + TypeScript 5 peer-dep conflict.
    Expected: workflow_config or dependency, fix targets the workflow or package file,
    fix_type is safe_auto_apply or review_recommended (not manual_required).
    """
    logs = (FIXTURES / "node_version_mismatch.log").read_text()
    d = await diagnose_failure(
        logs=logs,
        repo_full_name="aradhya/test-app",
        commit_message="Update dependencies",
        workflow_name="CI",
    )

    assert d.category in ("workflow_config", "dependency"), (
        f"Expected workflow_config or dependency, got {d.category}"
    )
    assert d.fix_type in ("safe_auto_apply", "review_recommended"), (
        f"Expected auto-fixable, got {d.fix_type}"
    )
    assert d.is_flaky_test is False, "Node version mismatch is not a flaky test"
    assert len(d.files_changed) > 0, "Should propose at least one file change"
    assert d.confidence >= 0.6, f"Confidence too low ({d.confidence}) for a clear dependency error"

    # The fix should touch a workflow file or package.json
    paths = [fc.path for fc in d.files_changed]
    touches_workflow_or_package = any(
        "workflow" in p or "package" in p or ".github" in p or ".yml" in p or ".yaml" in p
        for p in paths
    )
    assert touches_workflow_or_package, (
        f"Expected fix in workflow or package file, got {paths}"
    )


@pytest.mark.asyncio
async def test_missing_npm_dependency():
    """
    'Cannot find module jsonwebtoken' — missing from package.json.
    Expected: dependency category, safe_auto_apply or review_recommended, fix in package.json.
    """
    logs = (FIXTURES / "missing_npm_dep.log").read_text()
    d = await diagnose_failure(
        logs=logs,
        repo_full_name="aradhya/test-app",
        commit_message="Add auth tests",
        workflow_name="CI",
    )

    assert d.category == "dependency", f"Expected dependency, got {d.category}"
    assert d.fix_type in ("safe_auto_apply", "review_recommended"), (
        f"Missing dep should be fixable, got {d.fix_type}"
    )
    assert d.is_flaky_test is False
    assert len(d.files_changed) > 0, "Should propose a file change (package.json)"

    # Should mention jsonwebtoken in the diagnosis
    diagnosis_text = (d.problem_summary + d.root_cause + d.fix_description).lower()
    assert "jsonwebtoken" in diagnosis_text or "jwt" in diagnosis_text, (
        "Diagnosis should identify jsonwebtoken as the missing module"
    )


@pytest.mark.asyncio
async def test_flaky_test_detection():
    """
    Network timeouts + ETIMEDOUT in integration tests — flaky, not a real bug.
    Expected: is_flaky_test=True, fix_type=manual_required, files_changed=[].
    """
    logs = (FIXTURES / "flaky_test.log").read_text()
    d = await diagnose_failure(
        logs=logs,
        repo_full_name="aradhya/test-app",
        commit_message="Add payment integration tests",
        workflow_name="CI",
    )

    assert d.is_flaky_test is True, (
        "ETIMEDOUT + timeout in integration tests should be detected as flaky"
    )
    assert d.fix_type == "manual_required", (
        f"Flaky tests must not be auto-fixed, got {d.fix_type}"
    )
    assert d.files_changed == [], (
        f"Flaky test diagnosis must have no files_changed, got {d.files_changed}"
    )
    assert d.category in ("flaky_test", "environment"), (
        f"Expected flaky_test or environment category, got {d.category}"
    )


@pytest.mark.asyncio
async def test_truncated_logs():
    """
    Log that cuts off mid-stack-trace.
    Expected: logs_truncated_warning=True, confidence < 0.7.
    """
    logs = (FIXTURES / "truncated.log").read_text()
    d = await diagnose_failure(
        logs=logs,
        repo_full_name="aradhya/test-app",
        commit_message="Fix validation",
        workflow_name="CI",
    )

    assert d.logs_truncated_warning is True, (
        "Log cut off mid-stack-trace should set logs_truncated_warning=True"
    )
    assert d.confidence < 0.7, (
        f"Truncated logs should lower confidence, got {d.confidence}"
    )
