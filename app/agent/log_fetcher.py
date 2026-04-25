import io
import logging
import zipfile

import httpx

logger = logging.getLogger(__name__)

MAX_LOG_CHARS = 80_000


class LogFetchError(Exception):
    pass


class LogsNotAvailableError(LogFetchError):
    pass


class InsufficientPermissionsError(LogFetchError):
    pass


class LogsParseError(LogFetchError):
    pass


async def fetch_workflow_logs(github_run_id: int, repo_full_name: str, access_token: str) -> str:
    url = f"https://api.github.com/repos/{repo_full_name}/actions/runs/{github_run_id}/logs"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
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

    return _parse_zip_logs(response.content)


def _parse_zip_logs(zip_bytes: bytes) -> str:
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile as e:
        raise LogsParseError(f"Response was not a valid ZIP: {e}")

    txt_files = sorted(n for n in zf.namelist() if n.endswith(".txt"))
    if not txt_files:
        raise LogsParseError("ZIP contained no .txt log files")

    parts = []
    for fname in txt_files:
        try:
            content = zf.read(fname).decode("utf-8", errors="replace")
            parts.append(f"\n\n=== {fname} ===\n{content}")
        except Exception as e:
            logger.warning(f"Failed to read {fname} from log ZIP: {e}")

    if not parts:
        return "No logs available for this run."

    concatenated = "".join(parts)

    if len(concatenated) > MAX_LOG_CHARS:
        concatenated = "... [earlier logs truncated] ...\n" + concatenated[-MAX_LOG_CHARS:]

    return concatenated
