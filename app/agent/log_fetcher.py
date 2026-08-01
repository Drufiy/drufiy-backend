import asyncio
import io
import logging
import re
import zipfile

import httpx

logger = logging.getLogger(__name__)

MAX_LOG_CHARS = 80_000
MAX_LOG_ZIP_BYTES = 50_000_000
MAX_EXTRACTED_LOG_BYTES = 5_000_000
LOG_RETRY_ATTEMPTS = 3
LOG_RETRY_DELAY_SECONDS = 4


class LogFetchError(Exception):
    pass


class LogsNotAvailableError(LogFetchError):
    pass


class InsufficientPermissionsError(LogFetchError):
    pass


class LogsParseError(LogFetchError):
    pass

_MATRIX_JOB_RE = re.compile(r"^([^/]+)/")


def _extract_matrix_summary(filenames: list[str]) -> str:
    jobs: dict[str, int] = {}
    for fname in filenames:
        m = _MATRIX_JOB_RE.match(fname)
        if m:
            job_name = m.group(1).strip()
            jobs[job_name] = jobs.get(job_name, 0) + 1
    if len(jobs) <= 1:
        return ""
    lines = [f"- {name} ({count} steps)" for name, count in sorted(jobs.items())]
    return f"Multiple matrix jobs detected:\n" + "\n".join(lines)


# ── Log preprocessor ──────────────────────────────────────────────────────────
# Runs BEFORE the hard character-count truncation in _parse_zip_logs() (M3,
# ROADMAP.md "P1 BUG: Failure-blind log truncation") — a huge verbose job log
# (e.g. PHPUnit printing one [PASS] line per test across 2575 tests) gets
# shrunk to its error-relevant lines here, before length-based truncation even
# applies, so a failure buried early in a large log survives regardless of
# position. Previously lived in diagnosis_agent.py and ran AFTER truncation,
# which meant it could only rescue error lines that hadn't already been cut.

# Patterns that indicate an error line worth keeping
_ERROR_RE = re.compile(
    r"(error|fail|exception|traceback|panic|fatal|critical|warn"
    r"|cannot find|not found|does not exist|no such file"
    r"|exit code [^0]|npm err|pip.*error|cargo.*error"
    r"|enoent|eacces|eresolve|econnrefused|etimedout|enotfound"
    r"|syntaxerror|typeerror|referenceerror|importerror|modulenotfounderror"
    r"|✗|✕|FAILED|ERROR|WARN"
    r"|unable to find|unresolved|missing|undefined|undeclared"
    r"|permission denied|access denied|401|403|404 not found"
    r"|invalid|unexpected token|parse error|compilation failed"
    r"|resolutionimpossible|conflicting\s+dependencies|peer\s+dep|version\s+conflict"
    r"|COPY failed|docker.*build.*failed|manifest.*not found"
    r"|deploy.*failed|rollout.*failed|image.*push.*failed)",
    re.IGNORECASE,
)

_CONTEXT_LINES = 5  # lines of surrounding context to keep around each error line


def _preprocess_logs(raw: str) -> str:
    """
    Reduce verbose CI logs to error-relevant signal.
    Keeps lines matching error patterns + context, drops noisy setup output.
    Typically reduces 50KB → 5-10KB without losing the actual failure.
    """
    if not raw:
        return raw

    # Split into per-step sections on the === markers
    section_pattern = re.compile(r"(?m)^=== .+ ===$")
    splits = section_pattern.split(raw)
    headers = section_pattern.findall(raw)

    # If no section markers, treat entire log as one section
    if not headers:
        return _filter_section_lines(raw)

    result_parts = []
    for i, header in enumerate(headers):
        body = splits[i + 1] if i + 1 < len(splits) else ""
        filtered = _filter_section_lines(body)
        result_parts.append(f"{header}\n{filtered}")

    last_body = splits[-1] if splits else raw
    tail = "\n".join(last_body.splitlines()[-40:])
    return "\n\n".join(result_parts) + "\n\n=== RAW TAIL (last 40 lines) ===\n" + tail


def _filter_section_lines(section: str) -> str:
    lines = section.splitlines()
    if not lines:
        return section

    # Find indices of error lines
    error_idx: set[int] = set()
    for i, line in enumerate(lines):
        if _ERROR_RE.search(line):
            for j in range(max(0, i - _CONTEXT_LINES), min(len(lines), i + _CONTEXT_LINES + 1)):
                error_idx.add(j)

    if not error_idx:
        # No errors detected — keep last 20 lines (the conclusion of the step)
        return "\n".join(lines[-20:]) if len(lines) > 20 else section

    # Reconstruct with gap markers
    out: list[str] = []
    sorted_idx = sorted(error_idx)
    prev = -1
    for idx in sorted_idx:
        if prev != -1 and idx > prev + 1:
            gap = idx - prev - 1
            out.append(f"... [{gap} line{'s' if gap > 1 else ''} omitted] ...")
        out.append(lines[idx])
        prev = idx

    return "\n".join(out)


def _logs_have_step_output(zip_bytes: bytes) -> bool:
    """Check whether the ZIP contains actual job step output, not just orchestration."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile:
        return False
    txt_files = [n for n in zf.namelist() if n.endswith(".txt")]
    if len(txt_files) <= 1:
        return False
    for fname in txt_files:
        try:
            content = zf.read(fname).decode("utf-8", errors="replace")
            if "FAILED" in content or "error" in content.lower() or "passed" in content.lower():
                return True
        except Exception:
            continue
    return False


async def _fetch_failing_job_names(github_run_id: int, repo_full_name: str, access_token: str) -> set[str] | None:
    """
    Best-effort lookup of which jobs in this run actually failed, via the
    check-runs-adjacent jobs API. Returns None (not an empty set) on any
    failure so callers can tell "couldn't determine" apart from "nothing
    failed" and fall back to the old behavior rather than misbehave.
    """
    url = f"https://api.github.com/repos/{repo_full_name}/actions/runs/{github_run_id}/jobs"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(url, headers=headers, params={"per_page": 100})
        if response.status_code != 200:
            logger.warning(f"Could not fetch job list for run {github_run_id}: status {response.status_code}")
            return None
        jobs = response.json().get("jobs", [])
        return {j["name"] for j in jobs if j.get("conclusion") == "failure"}
    except Exception as e:
        logger.warning(f"Could not fetch job list for run {github_run_id}: {e}")
        return None


async def fetch_workflow_logs(github_run_id: int, repo_full_name: str, access_token: str) -> str:
    url = f"https://api.github.com/repos/{repo_full_name}/actions/runs/{github_run_id}/logs"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    zip_bytes = None
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        for attempt in range(LOG_RETRY_ATTEMPTS):
            response = await client.get(url, headers=headers)

            if response.status_code == 401:
                raise InsufficientPermissionsError("Token expired or invalid")
            if response.status_code == 403:
                raise InsufficientPermissionsError(f"Token lacks repo access for {repo_full_name}")
            if response.status_code == 404:
                raise LogsNotAvailableError(f"Logs for run {github_run_id} are not available (expired or deleted)")
            if response.status_code == 410:
                raise LogsNotAvailableError("Logs have been deleted (410 Gone)")
            if response.status_code != 200:
                raise LogFetchError(f"Unexpected status {response.status_code}: {response.text[:500]}")

            zip_bytes = response.content

            if _logs_have_step_output(zip_bytes):
                break

            if attempt < LOG_RETRY_ATTEMPTS - 1:
                logger.warning(
                    f"Log ZIP for run {github_run_id} has no step output yet "
                    f"(attempt {attempt + 1}/{LOG_RETRY_ATTEMPTS}), retrying in {LOG_RETRY_DELAY_SECONDS}s"
                )
                await asyncio.sleep(LOG_RETRY_DELAY_SECONDS)

    failing_job_names = await _fetch_failing_job_names(github_run_id, repo_full_name, access_token)
    return _parse_zip_logs(zip_bytes, failing_job_names=failing_job_names)


_TOP_LEVEL_JOB_RE = re.compile(r"^\d+_(.+)\.txt$")


def _job_name_for_file(path: str) -> str | None:
    """Extract a job's display name from either log-ZIP naming convention:
    a nested per-step file ('Backend (lint + test)/1_run.txt') or a flat
    per-job summary ('0_Backend (lint + test).txt')."""
    m = _MATRIX_JOB_RE.match(path)
    if m:
        return m.group(1).strip()
    m = _TOP_LEVEL_JOB_RE.match(path)
    if m:
        return m.group(1).strip()
    return None


def _parse_zip_logs(zip_bytes: bytes, failing_job_names: set[str] | None = None) -> str:
    if len(zip_bytes) > MAX_LOG_ZIP_BYTES:
        raise LogsParseError("Log ZIP is too large to process safely")

    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile as e:
        raise LogsParseError(f"Response was not a valid ZIP: {e}")

    # M2: failure-aware ordering. Truncation below keeps the LAST MAX_LOG_CHARS
    # of the concatenated blob, so failing jobs' files must sort last —
    # otherwise a large passing job can push a small failing job's entire log
    # out of the kept window, regardless of which job actually broke the build.
    # See ROADMAP.md "P1 BUG: Failure-blind log truncation".
    def _sort_key(fname: str) -> tuple:
        job_name = _job_name_for_file(fname)
        is_failing = bool(failing_job_names) and job_name in failing_job_names
        return (is_failing, fname)

    txt_files = sorted((n for n in zf.namelist() if n.endswith(".txt")), key=_sort_key)
    if not txt_files:
        raise LogsParseError("ZIP contained no .txt log files")

    parts = []
    for fname in txt_files:
        try:
            info = zf.getinfo(fname)
            if info.file_size > MAX_EXTRACTED_LOG_BYTES:
                logger.warning(f"Skipping oversized log file {fname} ({info.file_size} bytes)")
                continue
            content = zf.read(fname).decode("utf-8", errors="replace")
            parts.append(f"\n\n=== {fname} ===\n{content}")
        except Exception as e:
            logger.warning(f"Failed to read {fname} from log ZIP: {e}")

    if not parts:
        return "No logs available for this run."

    concatenated = "".join(parts)

    # M3: shrink to error-relevant signal BEFORE the hard length cut below —
    # see the "Log preprocessor" section above for why this ordering matters.
    concatenated = _preprocess_logs(concatenated)

    if len(concatenated) > MAX_LOG_CHARS:
        concatenated = "... [earlier logs truncated] ...\n" + concatenated[-MAX_LOG_CHARS:]

    matrix_summary = _extract_matrix_summary(zf.namelist())
    if matrix_summary:
        concatenated = f"=== MATRIX JOBS ===\n{matrix_summary}\n\n{concatenated}"

    return concatenated
