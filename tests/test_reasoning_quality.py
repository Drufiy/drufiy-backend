"""
Suite 5: Reasoning Quality
Tests that Kimi identifies root cause, not cascading symptoms.
Also tests iteration 2 reasoning — given a failed fix + new logs, what went wrong.
"""
import pytest
from app.agent.diagnosis_agent import diagnose_failure


CASCADE_LOG = """\n=== 1_Run tests ===\nFAIL src/__tests__/auth.test.ts\n  ✗ login returns JWT (2ms)\n    Cannot find module 'bcryptjs'\n\nFAIL src/__tests__/users.test.ts\n  ✗ creates user (1ms)\n    Cannot find module 'bcryptjs'\n\nFAIL src/__tests__/admin.test.ts\n  ✗ admin login works (1ms)\n    Cannot find module 'bcryptjs'\n\nFAIL src/__tests__/reset.test.ts\n  ✗ password reset sends email (1ms)\n    Cannot find module 'bcryptjs'\n\nFAIL src/__tests__/oauth.test.ts\n  ✗ github oauth callback (1ms)\n    Cannot find module 'bcryptjs'\n\n5 test suites failed, 5 tests failed.\nError: Process completed with exit code 1.\n"""

PYTHON_WRONG_VERSION = """\n=== 1_Set up Python ===\nSuccessfully set up Python 3.8.18\n\n=== 2_Install dependencies ===\nCollecting fastapi>=0.100.0\n  ERROR: Could not find a version that satisfies the requirement fastapi>=0.100.0\n  (from versions: 0.1.0, 0.2.0, ..., 0.99.1)\nERROR: No matching distribution found for fastapi>=0.100.0\nError: Process completed with exit code 1.\n"""

YAML_SYNTAX_LOG = """\n=== Parse workflow ===\nInvalid workflow file: .github/workflows/ci.yml\n\nError: .github/workflows/ci.yml (Line: 14, Col: 5):\nmapping values are not allowed in this context\n\n  12:     steps:\n  13:       - uses: actions/checkout@v4\n  14:       - name: Run tests\n  15:         run: npm test\n  16:     : bad_indent\nError: Process completed with exit code 1.\n"""

# Iteration 2 scenario: first fix tried wrong Node, but actual issue was npm ci flags
ITER2_ORIGINAL_LOGS = """\n=== 1_Run npm ci ===\nnpm ERR! code ERESOLVE\nnpm ERR! ERESOLVE unable to resolve dependency tree\nnpm ERR! Found: react@17.0.2\nnpm ERR! Could not resolve dependency: react@^18.0.0\nError: Process completed with exit code 1.\n"""

ITER2_PREVIOUS_DIAGNOSIS = {
    "problem_summary": "npm ci fails with ERESOLVE dependency conflict",
    "root_cause": "Node version 14 cannot handle the dependency resolution for react 18. Upgrading Node should fix the peer dependency resolution.",
    "fix_description": "Change node-version from 14 to 18 in .github/workflows/ci.yml",
    "files_changed": [{"path": ".github/workflows/ci.yml", "new_content": "name: CI\non: [push]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-node@v4\n        with:\n          node-version: '18'\n      - run: npm ci\n      - run: npm test\n", "explanation": "Changed node-version from 14 to 18"}],
}

ITER2_NEW_LOGS = """\n=== 1_actions_setup-node@v4 ===\nnode --version\nv18.20.2\n\n=== 2_Run npm ci ===\nnpm ERR! code ERESOLVE\nnpm ERR! ERESOLVE unable to resolve dependency tree\nnpm ERR! Found: react@17.0.2 in node_modules\nnpm ERR! But package.json requires react@^18.0.0\nnpm ERR!\nnpm ERR! Fix: update react in package.json from 17.0.2 to ^18.0.0\nError: Process completed with exit code 1.\n"""


@pytest.mark.asyncio
async def test_cascade_root_cause():
    """
    5 tests fail with the same 'Cannot find module bcryptjs'.
    Root cause is ONE missing dependency — not 5 separate bugs.
    """
    d = await diagnose_failure(
        logs=CASCADE_LOG,
        repo_full_name="test/repo",
        commit_message="Add auth module",
        workflow_name="CI",
    )
    # Should identify bcryptjs as the single root cause
    combined = (d.root_cause + d.fix_description + d.problem_summary).lower()
    assert "bcryptjs" in combined, (
        f"Must identify bcryptjs as root cause, not the 5 cascading failures. Got: {d.root_cause}"
    )
    assert d.category == "dependency"
    # Should fix ONE file (package.json), not 5 test files
    if d.files_changed:
        assert len(d.files_changed) == 1, (
            f"Fix should be in package.json only, not {[f.path for f in d.files_changed]}"
        )
        assert "package" in d.files_changed[0].path.lower() or "requirements" in d.files_changed[0].path.lower()


@pytest.mark.asyncio
async def test_python_version_incompatibility():
    """
    Python 3.8 + fastapi>=0.100.0 fails — fastapi dropped 3.8 support.
    Should identify: wrong Python version OR wrong fastapi version constraint.
    """
    d = await diagnose_failure(
        logs=PYTHON_WRONG_VERSION,
        repo_full_name="test/repo",
        commit_message="Upgrade fastapi",
        workflow_name="CI",
    )
    assert d.category in ("workflow_config", "dependency"), (
        f"Python version incompatibility should be workflow_config or dependency, got {d.category}"
    )
    assert d.fix_type in ("safe_auto_apply", "review_recommended")
    # Fix should mention either Python version or fastapi version
    combined = (d.root_cause + d.fix_description).lower()
    assert "python" in combined or "fastapi" in combined, (
        f"Diagnosis should mention Python or fastapi version, got: {d.root_cause}"
    )


@pytest.mark.asyncio
async def test_yaml_syntax_error():
    """
    YAML syntax error at line 16 of workflow file.
    Should pinpoint line 16 / indentation issue. fix_type should be safe_auto_apply.
    """
    d = await diagnose_failure(
        logs=YAML_SYNTAX_LOG,
        repo_full_name="test/repo",
        commit_message="Update workflow",
        workflow_name="CI",
    )
    assert d.category == "workflow_config", (
        f"YAML syntax in workflow file → workflow_config, got {d.category}"
    )
    assert d.fix_type in ("safe_auto_apply", "review_recommended")
    assert len(d.files_changed) > 0
    assert ".github" in d.files_changed[0].path or ".yml" in d.files_changed[0].path


@pytest.mark.asyncio
async def test_iteration_2_identifies_previous_mistake():
    """
    Previous fix changed Node version — but the real issue was react version in package.json.
    Iteration 2 must identify what the previous fix missed.
    """
    d = await diagnose_failure(
        logs=ITER2_NEW_LOGS,
        repo_full_name="test/repo",
        commit_message="fix CI (attempt 2)",
        workflow_name="CI",
        iteration=2,
        previous_diagnosis=ITER2_PREVIOUS_DIAGNOSIS,
    )
    # Must identify the actual root cause: react version in package.json
    combined = (d.root_cause + d.fix_description + d.problem_summary).lower()
    assert "react" in combined, (
        f"Iteration 2 must identify react version as the real issue. Got: {d.root_cause}"
    )
    assert d.category == "dependency"
    # Must NOT repeat the same Node version fix
    if d.files_changed:
        assert not all("workflow" in f.path for f in d.files_changed), (
            "Iteration 2 must not repeat the same workflow fix — must fix package.json"
        )
