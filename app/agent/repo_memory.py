import logging
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.db import supabase

logger = logging.getLogger(__name__)


@dataclass
class RepoMemory:
    repo_id: str
    similar_fixes: list[dict[str, Any]] = field(default_factory=list)
    repeated_error_signatures: list[dict[str, Any]] = field(default_factory=list)
    flaky_tests: list[dict[str, Any]] = field(default_factory=list)
    category_outcomes: dict[str, dict[str, Any]] = field(default_factory=dict)
    known_good_files: list[dict[str, Any]] = field(default_factory=list)
    dependency_patterns: list[dict[str, Any]] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not any(
            (
                self.similar_fixes,
                self.repeated_error_signatures,
                self.flaky_tests,
                self.category_outcomes,
                self.known_good_files,
                self.dependency_patterns,
            )
        )

    def as_prompt_context(self) -> str:
        if self.is_empty():
            return ""

        lines = [
            "\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            "REPO MEMORY (learned from this repository's previous CI failures and fixes)",
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        ]

        if self.category_outcomes:
            lines.append("\nHistorical outcomes by category:")
            for category, stats in sorted(self.category_outcomes.items()):
                attempts = stats.get("attempts", 0)
                verified = stats.get("verified", 0)
                exhausted = stats.get("exhausted", 0)
                rate = stats.get("verified_rate")
                rate_text = f"{int(rate * 100)}%" if isinstance(rate, float) else "unknown"
                lines.append(
                    f"- {category}: {verified}/{attempts} verified ({rate_text}), "
                    f"{exhausted} exhausted"
                )

        if self.repeated_error_signatures:
            lines.append("\nRepeated failure signatures:")
            for item in self.repeated_error_signatures:
                lines.append(
                    f"- signature={item.get('error_signature')} seen {item.get('count')}x, "
                    f"last category={item.get('last_category') or 'unknown'}, "
                    f"last status={item.get('last_status') or 'unknown'}"
                )

        if self.dependency_patterns:
            lines.append("\nDependency patterns that previously mattered:")
            for item in self.dependency_patterns:
                files = ", ".join(item.get("files", [])[:5]) or "unknown files"
                lines.append(f"- {item.get('summary', '')[:220]} (files: {files})")

        if self.flaky_tests:
            lines.append("\nKnown flaky tests:")
            for item in self.flaky_tests[:8]:
                test_name = item.get("test_name") or "*"
                lines.append(
                    f"- {item.get('test_file')}::{test_name}, "
                    f"failures={item.get('fail_count')}, pass-after-retry={item.get('pass_after_retry_count')}"
                )

        if self.known_good_files:
            lines.append("\nKnown-good files available for diff-risk comparison:")
            for item in self.known_good_files[:10]:
                lines.append(f"- {item.get('file_path')} verified_at={item.get('verified_at') or 'unknown'}")

        if self.similar_fixes:
            lines.append("\nRecent verified fixes from this repo:")
            for i, fix in enumerate(self.similar_fixes, 1):
                files_summary = ", ".join(
                    f.get("path", "") for f in (fix.get("files_changed") or []) if isinstance(f, dict)
                ) or "none"
                lines.append(
                    f"\nVerified Fix #{i} [{fix.get('category', '?')}] "
                    f"(confidence {int((fix.get('confidence') or 0) * 100)}%)"
                )
                lines.append(f"Problem: {fix.get('problem_summary', '')[:300]}")
                lines.append(f"Root cause: {fix.get('root_cause', '')[:300]}")
                lines.append(f"Fix: {fix.get('fix_description', '')[:300]}")
                lines.append(f"Files changed: {files_summary}")

        lines.append(
            "\nUse this memory as a prior, not as proof. If current logs contradict memory, trust the logs."
        )
        return "\n".join(lines)


def build_repo_memory(repo_id: str, limit: int = 5) -> RepoMemory:
    memory = RepoMemory(repo_id=repo_id)
    try:
        runs = _fetch_repo_runs(repo_id)
        run_ids = [r["id"] for r in runs if r.get("id")]
        if not run_ids:
            memory.flaky_tests = _fetch_flaky_tests(repo_id)
            memory.known_good_files = _fetch_known_good_files(repo_id)
            return memory

        diagnoses = _fetch_repo_diagnoses(run_ids)
        memory.similar_fixes = _recent_verified_fixes(diagnoses, limit=limit)
        memory.repeated_error_signatures = _repeated_error_signatures(diagnoses, runs)
        memory.category_outcomes = _category_outcomes(diagnoses)
        memory.dependency_patterns = _dependency_patterns(diagnoses, limit=limit)
        memory.flaky_tests = _fetch_flaky_tests(repo_id)
        memory.known_good_files = _fetch_known_good_files(repo_id)
        logger.info(
            "Repo memory built repo_id=%s fixes=%d signatures=%d flaky=%d categories=%d",
            repo_id,
            len(memory.similar_fixes),
            len(memory.repeated_error_signatures),
            len(memory.flaky_tests),
            len(memory.category_outcomes),
        )
        return memory
    except Exception as e:
        logger.warning(f"Repo memory build failed for repo_id={repo_id}: {e}")
        return memory


def _fetch_repo_runs(repo_id: str) -> list[dict[str, Any]]:
    result = (
        supabase.table("ci_runs")
        .select("id, status, created_at")
        .eq("repo_id", repo_id)
        .order("created_at", desc=True)
        .limit(200)
        .execute()
    )
    return result.data or []


def _fetch_repo_diagnoses(run_ids: list[str]) -> list[dict[str, Any]]:
    result = (
        supabase.table("diagnoses")
        .select(
            "id, run_id, problem_summary, root_cause, fix_description, category, confidence, "
            "files_changed, verification_status, error_signature, pr_merged_at, "
            "pr_closed_without_merge, reverted_within_7d, created_at"
        )
        .in_("run_id", run_ids)
        .order("created_at", desc=True)
        .limit(200)
        .execute()
    )
    return result.data or []


def _recent_verified_fixes(diagnoses: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    fixes = [
        _strip_internal_keys(d)
        for d in diagnoses
        if d.get("verification_status") == "verified" and d.get("files_changed")
    ]
    return fixes[:limit]


def _repeated_error_signatures(
    diagnoses: list[dict[str, Any]], runs: list[dict[str, Any]], limit: int = 5
) -> list[dict[str, Any]]:
    run_status = {r.get("id"): r.get("status") for r in runs}
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for diagnosis in diagnoses:
        signature = diagnosis.get("error_signature")
        if signature:
            grouped[signature].append(diagnosis)

    repeated = []
    for signature, items in grouped.items():
        if len(items) < 2:
            continue
        latest = sorted(items, key=lambda d: _parse_time(d.get("created_at")), reverse=True)[0]
        repeated.append(
            {
                "error_signature": signature,
                "count": len(items),
                "last_category": latest.get("category"),
                "last_status": latest.get("verification_status") or run_status.get(latest.get("run_id")),
            }
        )
    repeated.sort(key=lambda item: item["count"], reverse=True)
    return repeated[:limit]


def _category_outcomes(diagnoses: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    stats: dict[str, Counter] = defaultdict(Counter)
    for diagnosis in diagnoses:
        category = diagnosis.get("category") or "unknown"
        stats[category]["attempts"] += 1
        if diagnosis.get("verification_status") == "verified" or diagnosis.get("pr_merged_at"):
            stats[category]["verified"] += 1
        if diagnosis.get("pr_closed_without_merge"):
            stats[category]["rejected"] += 1
        if diagnosis.get("reverted_within_7d"):
            stats[category]["reverted"] += 1
        if diagnosis.get("verification_status") == "failed":
            stats[category]["failed"] += 1
        run_failed = diagnosis.get("verification_status") == "failed" or diagnosis.get("pr_closed_without_merge")
        if run_failed and not diagnosis.get("pr_merged_at"):
            stats[category]["exhausted"] += 1

    result = {}
    for category, counter in stats.items():
        attempts = counter["attempts"]
        verified = counter["verified"]
        result[category] = {
            "attempts": attempts,
            "verified": verified,
            "failed": counter["failed"],
            "rejected": counter["rejected"],
            "reverted": counter["reverted"],
            "exhausted": counter["exhausted"],
            "verified_rate": round(verified / attempts, 2) if attempts else None,
        }
    return result


def _dependency_patterns(diagnoses: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    patterns = []
    for diagnosis in diagnoses:
        if diagnosis.get("category") != "dependency":
            continue
        files = [
            f.get("path")
            for f in (diagnosis.get("files_changed") or [])
            if isinstance(f, dict) and f.get("path")
        ]
        patterns.append(
            {
                "summary": diagnosis.get("fix_description") or diagnosis.get("problem_summary") or "",
                "files": files,
                "verified": diagnosis.get("verification_status") == "verified",
            }
        )
    return patterns[:limit]


def _fetch_flaky_tests(repo_id: str) -> list[dict[str, Any]]:
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


def _fetch_known_good_files(repo_id: str) -> list[dict[str, Any]]:
    result = (
        supabase.table("known_good_files")
        .select("file_path, verified_at")
        .eq("repo_id", repo_id)
        .order("verified_at", desc=True)
        .limit(20)
        .execute()
    )
    return result.data or []


def _strip_internal_keys(diagnosis: dict[str, Any]) -> dict[str, Any]:
    return {
        k: v
        for k, v in diagnosis.items()
        if k
        not in {
            "id",
            "run_id",
            "pr_merged_at",
            "pr_closed_without_merge",
            "reverted_within_7d",
            "created_at",
        }
    }


def _parse_time(value: str | None) -> datetime:
    if not value:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
