import pytest

from app.agent.diagnosis_agent import diagnose_failure


def _base_args(**overrides):
    data = {
        "problem_summary": "CI fails because lodash cannot be resolved",
        "root_cause": "The runner reports Cannot find module 'lodash', which means the package is missing in the CI environment.",
        "fix_description": "Add the missing lodash dependency before running the script.",
        "fix_type": "safe_auto_apply",
        "confidence": 0.93,
        "is_flaky_test": False,
        "files_changed": [
            {
                "path": "src/lodash.js",
                "new_content": "module.exports = {};",
                "explanation": "Incorrectly creates a dummy module.",
            }
        ],
        "category": "code",
        "logs_truncated_warning": False,
    }
    data.update(overrides)
    return data


@pytest.mark.asyncio
async def test_bare_missing_module_downgrades_source_rewrite(monkeypatch):
    async def fake_call(**kwargs):
        return _base_args()

    monkeypatch.setattr("app.agent.diagnosis_agent.call_with_tool", fake_call)
    diagnosis = await diagnose_failure(
        logs="Error: Cannot find module 'lodash'\nRequire stack:\n- index.js",
        repo_full_name="test/repo",
        commit_message="test",
        workflow_name="CI",
        model="unit",
    )

    assert diagnosis.fix_type == "review_recommended"
    assert diagnosis.speculative is True
    assert "missing package/module" in diagnosis.fix_description


@pytest.mark.asyncio
async def test_bare_missing_module_allows_manifest_fix(monkeypatch):
    async def fake_call(**kwargs):
        return _base_args(
            files_changed=[
                {
                    "path": "package.json",
                    "new_content": '{"dependencies":{"lodash":"^4.17.21"}}',
                    "explanation": "Adds lodash.",
                }
            ],
            category="dependency",
        )

    monkeypatch.setattr("app.agent.diagnosis_agent.call_with_tool", fake_call)
    diagnosis = await diagnose_failure(
        logs="Error: Cannot find module 'lodash'",
        repo_full_name="test/repo",
        commit_message="test",
        workflow_name="CI",
        model="unit",
    )

    assert diagnosis.fix_type == "safe_auto_apply"
    assert diagnosis.category == "dependency"


@pytest.mark.asyncio
async def test_missing_secret_is_extracted_even_if_model_omits_it(monkeypatch):
    async def fake_call(**kwargs):
        return _base_args(
            problem_summary="Deploy fails because Stripe secret is missing",
            root_cause="STRIPE_SECRET_KEY is not defined in the GitHub Actions environment.",
            fix_description="Add the missing secret.",
            files_changed=[],
            fix_type="manual_required",
            category="unknown",
            confidence=0.91,
        )

    monkeypatch.setattr("app.agent.diagnosis_agent.call_with_tool", fake_call)
    diagnosis = await diagnose_failure(
        logs="Error: STRIPE_SECRET_KEY is not defined",
        repo_full_name="test/repo",
        commit_message="test",
        workflow_name="CI",
        model="unit",
    )

    assert diagnosis.category == "environment"
    assert diagnosis.fix_type == "manual_required"
    assert diagnosis.required_secrets == ["STRIPE_SECRET_KEY"]
    assert diagnosis.files_changed == []


@pytest.mark.asyncio
async def test_docker_copy_wrong_path_is_downgraded(monkeypatch):
    async def fake_call(**kwargs):
        return _base_args(
            problem_summary="Docker build cannot copy a missing file",
            root_cause="Docker COPY ./non_existent_file.txt fails because the file is absent from the build context.",
            fix_description="Create the missing file.",
            files_changed=[
                {
                    "path": "src/non_existent_file.txt",
                    "new_content": "placeholder",
                    "explanation": "Wrong path for the Docker build context.",
                }
            ],
            category="code",
            confidence=0.9,
        )

    monkeypatch.setattr("app.agent.diagnosis_agent.call_with_tool", fake_call)
    diagnosis = await diagnose_failure(
        logs="#7 [3/4] COPY ./non_existent_file.txt /app/\nERROR: failed to calculate checksum: file not found",
        repo_full_name="test/repo",
        commit_message="test",
        workflow_name="Docker",
        model="unit",
    )

    assert diagnosis.fix_type == "review_recommended"
    assert diagnosis.speculative is True
    assert "Docker reported missing build-context path" in diagnosis.fix_description


@pytest.mark.asyncio
async def test_docker_copy_exact_missing_path_stays_safe(monkeypatch):
    async def fake_call(**kwargs):
        return _base_args(
            problem_summary="Docker build cannot copy a missing file",
            root_cause="Docker COPY ./non_existent_file.txt fails because the file is absent from the build context.",
            fix_description="Create the missing file at the Docker build-context path.",
            files_changed=[
                {
                    "path": "non_existent_file.txt",
                    "new_content": "placeholder",
                    "explanation": "Creates the file Docker is copying from the build context.",
                }
            ],
            category="code",
            confidence=0.9,
        )

    monkeypatch.setattr("app.agent.diagnosis_agent.call_with_tool", fake_call)
    diagnosis = await diagnose_failure(
        logs="#7 [3/4] COPY ./non_existent_file.txt /app/\nERROR: failed to calculate checksum: file not found",
        repo_full_name="test/repo",
        commit_message="test",
        workflow_name="Docker",
        model="unit",
    )

    assert diagnosis.fix_type == "safe_auto_apply"
    assert diagnosis.speculative is False
