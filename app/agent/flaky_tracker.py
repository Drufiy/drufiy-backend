import logging
from datetime import datetime, timezone

from app.db import supabase

logger = logging.getLogger(__name__)


def is_known_flaky(repo_id: str, test_file: str, test_name: str | None = None) -> bool:
    try:
        q = (
            supabase.table("flaky_tests")
            .select("id, fail_count")
            .eq("repo_id", repo_id)
            .eq("test_file", test_file)
            .eq("is_active", True)
        )
        if test_name:
            q = q.eq("test_name", test_name)
        result = q.limit(1).execute()
        return bool(result.data)
    except Exception as e:
        logger.warning(f"flaky_tracker lookup failed: {e}")
        return False


def record_flaky(repo_id: str, test_file: str, test_name: str | None = None):
    now = datetime.now(timezone.utc).isoformat()
    try:
        existing = (
            supabase.table("flaky_tests")
            .select("id, fail_count, pass_after_retry_count")
            .eq("repo_id", repo_id)
            .eq("test_file", test_file)
        )
        if test_name:
            existing = existing.eq("test_name", test_name)
        existing = existing.limit(1).execute()

        if existing.data:
            row = existing.data[0]
            supabase.table("flaky_tests").update({
                "fail_count": row["fail_count"] + 1,
                "pass_after_retry_count": row["pass_after_retry_count"] + 1,
                "last_seen_at": now,
                "is_active": True,
            }).eq("id", row["id"]).execute()
        else:
            supabase.table("flaky_tests").insert({
                "repo_id": repo_id,
                "test_file": test_file,
                "test_name": test_name,
                "fail_count": 1,
                "pass_after_retry_count": 1,
                "last_seen_at": now,
                "first_seen_at": now,
                "is_active": True,
            }).execute()
        logger.info(f"Recorded flaky test: {test_file}::{test_name or '*'} in repo {repo_id[:8]}")
    except Exception as e:
        logger.warning(f"Failed to record flaky test: {e}")


def record_flaky_failure_only(repo_id: str, test_file: str, test_name: str | None = None):
    now = datetime.now(timezone.utc).isoformat()
    try:
        existing = (
            supabase.table("flaky_tests")
            .select("id, fail_count")
            .eq("repo_id", repo_id)
            .eq("test_file", test_file)
        )
        if test_name:
            existing = existing.eq("test_name", test_name)
        existing = existing.limit(1).execute()

        if existing.data:
            row = existing.data[0]
            supabase.table("flaky_tests").update({
                "fail_count": row["fail_count"] + 1,
                "last_seen_at": now,
                "is_active": True,
            }).eq("id", row["id"]).execute()
        else:
            supabase.table("flaky_tests").insert({
                "repo_id": repo_id,
                "test_file": test_file,
                "test_name": test_name,
                "fail_count": 1,
                "pass_after_retry_count": 0,
                "last_seen_at": now,
                "first_seen_at": now,
                "is_active": True,
            }).execute()
    except Exception as e:
        logger.warning(f"Failed to record flaky failure: {e}")


def get_flaky_summary(repo_id: str) -> list[dict]:
    try:
        result = (
            supabase.table("flaky_tests")
            .select("test_file, test_name, fail_count, pass_after_retry_count, last_seen_at")
            .eq("repo_id", repo_id)
            .eq("is_active", True)
            .order("fail_count", desc=True)
            .limit(20)
            .execute()
        )
        return result.data or []
    except Exception as e:
        logger.warning(f"Failed to fetch flaky summary: {e}")
        return []
