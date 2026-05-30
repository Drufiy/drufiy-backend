import os

import pytest


os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault(
    "SUPABASE_SERVICE_KEY",
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJyb2xlIjoic2VydmljZV9yb2xlIiwiZXhwIjoyMDAwMDAwMDAwfQ.signature",
)
os.environ.setdefault("KIMI_API_KEY", "test-kimi-key")
os.environ.setdefault("GITHUB_CLIENT_ID", "test-client-id")
os.environ.setdefault("GITHUB_CLIENT_SECRET", "test-client-secret")
os.environ.setdefault("GITHUB_WEBHOOK_SECRET", "test-webhook-secret")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret")


LIVE_AI_MODULES = {
    "test_diagnosis.py",
    "test_fix_type_discipline.py",
    "test_hallucination.py",
    "test_kimi_client.py",
    "test_reasoning_quality.py",
    "test_speed.py",
    "test_tool_calling.py",
}


def pytest_collection_modifyitems(config, items):
    run_live_ai = os.getenv("RUN_LIVE_AI_TESTS") == "1"
    has_real_key = os.getenv("KIMI_API_KEY") not in ("", "test-kimi-key")
    if run_live_ai and has_real_key:
        return

    skip_live_ai = pytest.mark.skip(
        reason="Set RUN_LIVE_AI_TESTS=1 with KIMI_API_KEY to run live Kimi evals"
    )
    for item in items:
        if item.path.name in LIVE_AI_MODULES and item.get_closest_marker("asyncio"):
            item.add_marker(skip_live_ai)
