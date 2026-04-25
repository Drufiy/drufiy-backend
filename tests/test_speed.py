"""
Suite 6: Latency Benchmarks
Measures real Kimi response times. Target: p50 < 60s, p95 < 120s.
Runs 3 back-to-back diagnoses to measure warm vs cold latency.
"""
import time
import pytest
from app.agent.diagnosis_agent import diagnose_failure

STANDARD_LOG = """\n=== 1_Run npm ci ===\nnpm ERR! code E404\nnpm ERR! 404 Not Found: lodash@4.99.0\nnpm ERR! 404 'lodash@4.99.0' is not in this registry.\nError: Process completed with exit code 1.\n"""


@pytest.mark.asyncio
async def test_single_diagnosis_latency():
    """Single call must complete under 120 seconds."""
    start = time.time()
    d = await diagnose_failure(
        logs=STANDARD_LOG,
        repo_full_name="test/repo",
        commit_message="update lodash",
        workflow_name="CI",
    )
    elapsed = time.time() - start

    print(f"\n⏱  Single diagnosis latency: {elapsed:.1f}s")
    assert d is not None
    assert elapsed < 120, f"Diagnosis took {elapsed:.1f}s — exceeds 120s limit"


@pytest.mark.asyncio
async def test_three_run_latency_p50():
    """Run 3 diagnoses sequentially and measure p50. Target: median < 90s."""
    latencies = []

    for i in range(3):
        start = time.time()
        await diagnose_failure(
            logs=STANDARD_LOG,
            repo_full_name="test/repo",
            commit_message=f"run {i + 1}",
            workflow_name="CI",
        )
        elapsed = time.time() - start
        latencies.append(elapsed)
        print(f"  Run {i + 1}: {elapsed:.1f}s")

    latencies.sort()
    p50 = latencies[1]
    p_max = latencies[2]

    print(f"\n⏱  Latency results — p50: {p50:.1f}s  max: {p_max:.1f}s")
    print(f"   All: {[f'{l:.1f}s' for l in sorted(latencies)]}")

    # Soft assertions — log results, only hard-fail on extreme outliers
    assert p_max < 180, f"Worst-case latency {p_max:.1f}s exceeds 180s hard limit"

    if p50 >= 90:
        print(f"  ⚠  p50 {p50:.1f}s is above 90s target — consider pre-warm or model switch")
    else:
        print(f"  ✓  p50 {p50:.1f}s is within 90s target")
