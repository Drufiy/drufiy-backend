"""Scoring rubric for the golden benchmark.

The metrics are deliberately ordered by what the production data says matters most:

  1. valid_diagnosis  — did the model return a schema-valid Diagnosis at all?
                        (29% of prod runs die here — this is the headline number)
  2. category_match   — correct failure classification (drives routing)
  3. actionability    — did it produce a fix when a fix was the right call?
  4. file_recall      — did it target the right file(s)?
  5. fix_type_match   — exact fix_type agreement (softer signal)
  6. latency_ms       — wall-clock per diagnosis

Ground truth is only trusted for cases with source == "verified". Negative cases
score only `valid_diagnosis` (we have no known-good fix for them).
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict


@dataclass
class CaseResult:
    case_id: str
    source: str
    valid_diagnosis: bool
    error: str | None = None
    latency_ms: int | None = None

    # populated only when a diagnosis was produced
    predicted_category: str | None = None
    expected_category: str | None = None
    predicted_fix_type: str | None = None
    expected_fix_type: str | None = None
    produced_files: list[str] = field(default_factory=list)
    expected_files: list[str] = field(default_factory=list)

    # derived
    category_match: bool | None = None
    fix_type_match: bool | None = None
    actionable_match: bool | None = None
    file_recall: float | None = None

    def score(self) -> "CaseResult":
        if not self.valid_diagnosis or self.source != "verified":
            return self
        self.category_match = self.predicted_category == self.expected_category
        self.fix_type_match = self.predicted_fix_type == self.expected_fix_type
        # "actionable" = both sides agree on whether a code fix should exist
        exp_actionable = self.expected_fix_type in ("safe_auto_apply", "review_recommended")
        got_actionable = bool(self.produced_files)
        self.actionable_match = exp_actionable == got_actionable
        if self.expected_files:
            hit = sum(1 for p in self.expected_files if p in self.produced_files)
            self.file_recall = round(hit / len(self.expected_files), 3)
        return self

    def to_dict(self) -> dict:
        return asdict(self)


def aggregate(results: list[CaseResult]) -> dict:
    n = len(results)
    verified = [r for r in results if r.source == "verified"]
    diagnosed = [r for r in results if r.valid_diagnosis]
    ver_diag = [r for r in verified if r.valid_diagnosis]

    def pct(num, den):
        return round(100 * num / den, 1) if den else None

    lats = [r.latency_ms for r in results if r.latency_ms is not None]
    lats.sort()

    def p(q):
        return lats[min(int(len(lats) * q), len(lats) - 1)] if lats else None

    file_recalls = [r.file_recall for r in ver_diag if r.file_recall is not None]

    return {
        "n_cases": n,
        "valid_diagnosis_rate_pct": pct(len(diagnosed), n),          # <- headline
        "verified_cohort": {
            "n": len(verified),
            "valid_diagnosis_pct": pct(len(ver_diag), len(verified)),
            "category_accuracy_pct": pct(sum(1 for r in ver_diag if r.category_match), len(ver_diag)),
            "actionability_pct": pct(sum(1 for r in ver_diag if r.actionable_match), len(ver_diag)),
            "fix_type_accuracy_pct": pct(sum(1 for r in ver_diag if r.fix_type_match), len(ver_diag)),
            "mean_file_recall": round(sum(file_recalls) / len(file_recalls), 3) if file_recalls else None,
        },
        "latency_ms": {"p50": p(0.5), "p90": p(0.9), "max": lats[-1] if lats else None},
        "errors": [{"case": r.case_id, "error": r.error} for r in results if not r.valid_diagnosis],
    }


def render_scorecard(agg: dict, label: str) -> str:
    vc = agg["verified_cohort"]
    lat = agg["latency_ms"]
    lines = [
        f"\n┌─ EVAL SCORECARD — {label} " + "─" * max(0, 40 - len(label)),
        f"│ cases: {agg['n_cases']}   valid_diagnosis: {agg['valid_diagnosis_rate_pct']}%   "
        f"(THE headline metric — prod is ~71%)",
        f"│ verified cohort (n={vc['n']}):",
        f"│    valid_diagnosis : {vc['valid_diagnosis_pct']}%",
        f"│    category_acc    : {vc['category_accuracy_pct']}%",
        f"│    actionability   : {vc['actionability_pct']}%   (produced a fix when one was expected)",
        f"│    fix_type_acc    : {vc['fix_type_accuracy_pct']}%",
        f"│    file_recall     : {vc['mean_file_recall']}",
        f"│ latency: p50={lat['p50']}ms  p90={lat['p90']}ms  max={lat['max']}ms",
        f"│ failures: {len(agg['errors'])}",
        "└" + "─" * 50,
    ]
    for e in agg["errors"]:
        lines.append(f"   ✗ {e['case']}: {e['error']}")
    return "\n".join(lines)
