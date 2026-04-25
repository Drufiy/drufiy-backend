import difflib
import logging
from dataclasses import dataclass

from app.db import supabase

logger = logging.getLogger(__name__)


@dataclass
class DiffRisk:
    file_path: str
    has_known_good: bool
    changed_regions: int
    lines_added: int
    lines_removed: int
    risk_level: str   # 'low' | 'medium' | 'high'
    risk_reason: str


async def assess_diff_risk(repo_id: str, file_path: str, proposed_content: str) -> DiffRisk:
    result = (
        supabase.table("known_good_files")
        .select("content")
        .eq("repo_id", repo_id)
        .eq("file_path", file_path)
        .limit(1)
        .execute()
    )

    if not result.data:
        return DiffRisk(
            file_path=file_path,
            has_known_good=False,
            changed_regions=0,
            lines_added=len(proposed_content.splitlines()),
            lines_removed=0,
            risk_level="medium",
            risk_reason="No known-good version to compare against (first time seeing this file)",
        )

    known = result.data[0]["content"]
    old_lines = known.splitlines()
    new_lines = proposed_content.splitlines()

    matcher = difflib.SequenceMatcher(None, old_lines, new_lines)
    opcodes = matcher.get_opcodes()

    changed_regions = sum(1 for tag, *_ in opcodes if tag != "equal")
    lines_added = sum(j2 - j1 for tag, _, _, j1, j2 in opcodes if tag in ("insert", "replace"))
    lines_removed = sum(i2 - i1 for tag, i1, i2, _, _ in opcodes if tag in ("delete", "replace"))

    total_lines = max(len(old_lines), 1)
    change_ratio = (lines_added + lines_removed) / total_lines

    if changed_regions == 0:
        risk_level, reason = "low", "No changes vs known-good"
    elif changed_regions == 1 and change_ratio < 0.1:
        risk_level, reason = "low", "Single contiguous change, <10% of file"
    elif changed_regions <= 2 and change_ratio < 0.25:
        risk_level, reason = "medium", f"{changed_regions} changed regions, {change_ratio:.0%} of file modified"
    else:
        risk_level, reason = (
            "high",
            f"{changed_regions} separate changed regions, {change_ratio:.0%} of file modified — possible hallucination",
        )

    return DiffRisk(
        file_path=file_path,
        has_known_good=True,
        changed_regions=changed_regions,
        lines_added=lines_added,
        lines_removed=lines_removed,
        risk_level=risk_level,
        risk_reason=reason,
    )
