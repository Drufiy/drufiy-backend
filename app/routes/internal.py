"""
Internal endpoints — called by Cloud Scheduler or admin tooling.
Protected by X-Internal-Secret header matching settings.internal_cron_secret.
Never exposed to end-users.
"""

import logging
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import JSONResponse

from app.config import settings
from app.db import supabase

logger = logging.getLogger(__name__)
router = APIRouter()

GITHUB_API = "https://api.github.com"


# ── Auth guard ────────────────────────────────────────────────────────────────

def _require_cron_secret(x_internal_secret: str | None = Header(default=None)):
    if not settings.internal_cron_secret:
        raise HTTPException(status_code=503, detail="Internal cron secret not configured")
    if x_internal_secret != settings.internal_cron_secret:
        raise HTTPException(status_code=401, detail="Invalid internal secret")


# ── Weekly report logic ───────────────────────────────────────────────────────

def _build_weekly_stats(since_iso: str) -> list[dict]:
    """
    Aggregate ci_runs from the last 7 days, grouped by user.
    Returns a list of per-user report dicts.
    """
    runs_resp = (
        supabase.table("ci_runs")
        .select("id, status, created_at, connected_repo_id, connected_repos(repo_full_name, user_id)")
        .gte("created_at", since_iso)
        .execute()
    )
    runs = runs_resp.data or []
    if not runs:
        return []

    # Group by user_id
    from collections import defaultdict
    user_runs: dict[str, list[dict]] = defaultdict(list)
    for run in runs:
        repo = run.get("connected_repos") or {}
        uid = repo.get("user_id")
        if uid:
            user_runs[uid].append(run)

    # Fetch user profiles (email + github_username)
    user_ids = list(user_runs.keys())
    profiles_resp = (
        supabase.table("user_profiles")
        .select("id, email, github_username")
        .in_("id", user_ids)
        .execute()
    )
    profiles = {p["id"]: p for p in (profiles_resp.data or [])}

    reports = []
    for uid, user_run_list in user_runs.items():
        profile = profiles.get(uid, {})
        total = len(user_run_list)
        by_status: dict[str, int] = {}
        for run in user_run_list:
            s = run.get("status", "unknown")
            by_status[s] = by_status.get(s, 0) + 1

        verified = by_status.get("verified", 0) + by_status.get("merged", 0)
        fixed = by_status.get("fixed", 0)
        exhausted = by_status.get("exhausted", 0)
        fix_rate = round((verified + fixed) / total * 100) if total else 0

        # Per-repo breakdown
        repo_counts: dict[str, int] = {}
        for run in user_run_list:
            repo_name = (run.get("connected_repos") or {}).get("repo_full_name", "unknown")
            repo_counts[repo_name] = repo_counts.get(repo_name, 0) + 1
        top_repos = sorted(repo_counts.items(), key=lambda x: x[1], reverse=True)[:5]

        reports.append({
            "user_id": uid,
            "email": profile.get("email"),
            "github_username": profile.get("github_username"),
            "total_runs": total,
            "verified": verified,
            "fixed_pending": fixed,
            "exhausted": exhausted,
            "fix_rate_pct": fix_rate,
            "by_status": by_status,
            "top_repos": [{"repo": r, "runs": c} for r, c in top_repos],
        })

    return reports


def _build_email_html(report: dict, week_label: str) -> str:
    username = report.get("github_username") or "there"
    fix_rate = report["fix_rate_pct"]
    color = "#22c55e" if fix_rate >= 70 else "#f59e0b" if fix_rate >= 40 else "#ef4444"
    repos_html = "".join(
        f"<tr><td style='padding:4px 8px;border-bottom:1px solid #e5e7eb'>{r['repo']}</td>"
        f"<td style='padding:4px 8px;border-bottom:1px solid #e5e7eb;text-align:center'>{r['runs']}</td></tr>"
        for r in report["top_repos"]
    )
    return f"""
<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f9fafb;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif">
  <div style="max-width:600px;margin:32px auto;background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.1)">

    <!-- Header -->
    <div style="background:#0f172a;padding:24px 32px;display:flex;align-items:center">
      <span style="color:#fff;font-size:22px;font-weight:700">🛠 Drufiy</span>
      <span style="color:#94a3b8;font-size:14px;margin-left:12px">Weekly CI Report</span>
    </div>

    <!-- Body -->
    <div style="padding:32px">
      <p style="margin:0 0 24px;color:#374151;font-size:16px">Hey @{username} 👋, here's your CI health summary for <strong>{week_label}</strong>.</p>

      <!-- Stats row -->
      <div style="display:flex;gap:16px;margin-bottom:32px">
        <div style="flex:1;background:#f8fafc;border-radius:8px;padding:16px;text-align:center;border:1px solid #e2e8f0">
          <div style="font-size:32px;font-weight:700;color:#0f172a">{report['total_runs']}</div>
          <div style="font-size:13px;color:#64748b;margin-top:4px">CI Failures</div>
        </div>
        <div style="flex:1;background:#f8fafc;border-radius:8px;padding:16px;text-align:center;border:1px solid #e2e8f0">
          <div style="font-size:32px;font-weight:700;color:{color}">{fix_rate}%</div>
          <div style="font-size:13px;color:#64748b;margin-top:4px">Fix Rate</div>
        </div>
        <div style="flex:1;background:#f8fafc;border-radius:8px;padding:16px;text-align:center;border:1px solid #e2e8f0">
          <div style="font-size:32px;font-weight:700;color:#22c55e">{report['verified']}</div>
          <div style="font-size:13px;color:#64748b;margin-top:4px">Verified Fixes</div>
        </div>
        <div style="flex:1;background:#f8fafc;border-radius:8px;padding:16px;text-align:center;border:1px solid #e2e8f0">
          <div style="font-size:32px;font-weight:700;color:#f59e0b">{report['exhausted']}</div>
          <div style="font-size:13px;color:#64748b;margin-top:4px">Need Review</div>
        </div>
      </div>

      <!-- Top repos -->
      {'<h3 style="margin:0 0 12px;font-size:15px;color:#0f172a">Most Active Repos</h3><table style="width:100%;border-collapse:collapse;font-size:14px"><thead><tr><th style="padding:6px 8px;text-align:left;color:#64748b;font-weight:600;border-bottom:2px solid #e5e7eb">Repository</th><th style="padding:6px 8px;text-align:center;color:#64748b;font-weight:600;border-bottom:2px solid #e5e7eb">Runs</th></tr></thead><tbody>' + repos_html + '</tbody></table>' if report['top_repos'] else ''}

      <div style="margin-top:32px;text-align:center">
        <a href="{settings.frontend_url}/dashboard" style="display:inline-block;background:#0f172a;color:#fff;text-decoration:none;padding:12px 28px;border-radius:8px;font-size:14px;font-weight:600">View Dashboard →</a>
      </div>
    </div>

    <!-- Footer -->
    <div style="padding:16px 32px;background:#f8fafc;border-top:1px solid #e5e7eb;text-align:center">
      <p style="margin:0;font-size:12px;color:#94a3b8">
        You're receiving this because you connected repos to <a href="{settings.frontend_url}" style="color:#64748b">Drufiy</a>.
        This email is sent every Monday morning.
      </p>
    </div>
  </div>
</body>
</html>
"""


async def _send_resend_email(to_email: str, subject: str, html: str) -> bool:
    """Send via Resend HTTP API. Returns True on success."""
    if not settings.resend_api_key:
        logger.info(f"Resend not configured — skipping email to {to_email}")
        return False
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                "https://api.resend.com/emails",
                headers={
                    "Authorization": f"Bearer {settings.resend_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "from": settings.report_email_from,
                    "to": [to_email],
                    "subject": subject,
                    "html": html,
                },
            )
        if resp.status_code in (200, 201):
            logger.info(f"Weekly report emailed to {to_email}")
            return True
        logger.warning(f"Resend returned {resp.status_code} for {to_email}: {resp.text[:200]}")
        return False
    except Exception as e:
        logger.warning(f"Resend send failed for {to_email}: {e}")
        return False


# ── Endpoint ──────────────────────────────────────────────────────────────────

@router.post("/weekly-report")
async def trigger_weekly_report(
    x_internal_secret: str | None = Header(default=None),
    dry_run: bool = False,
):
    """
    Triggered by Cloud Scheduler every Monday at 09:00 IST.
    Generates per-user weekly CI health stats and sends email if Resend is configured.
    Pass ?dry_run=true to return the report JSON without sending emails.
    """
    _require_cron_secret(x_internal_secret)

    since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    reports = _build_weekly_stats(since)

    if not reports:
        return {"status": "no_data", "message": "No CI runs in the last 7 days"}

    now = datetime.now(timezone.utc)
    week_end = now.strftime("%b %d")
    week_start = (now - timedelta(days=7)).strftime("%b %d")
    week_label = f"{week_start} – {week_end}, {now.year}"

    results = []
    for report in reports:
        email = report.get("email")
        sent = False
        if email and not dry_run:
            subject = f"Drufiy Weekly Report: {report['fix_rate_pct']}% fix rate this week"
            html = _build_email_html(report, week_label)
            sent = await _send_resend_email(email, subject, html)

        results.append({
            "user": report.get("github_username"),
            "email": email,
            "email_sent": sent,
            "stats": {
                "total_runs": report["total_runs"],
                "fix_rate_pct": report["fix_rate_pct"],
                "verified": report["verified"],
                "exhausted": report["exhausted"],
            },
        })

    return {
        "status": "ok",
        "week": week_label,
        "users_processed": len(results),
        "emails_sent": sum(1 for r in results if r["email_sent"]),
        "dry_run": dry_run,
        "results": results,
    }
