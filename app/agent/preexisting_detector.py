import re

# Lines that signal a failure worth inspecting for an implicated file.
_ERR_LINE_RE = re.compile(
    r"error|fail|cannot find|not found|cannot resolve|module not found"
    r"|no such file|undefined|is not defined|unresolved",
    re.IGNORECASE,
)

# Source-file paths (with a known extension) referenced in/near an error line.
_FILE_IN_ERROR_RE = re.compile(
    r"([\w./\-]+\.(?:tsx?|jsx?|py|go|rs|java|rb|php|cs|vue|svelte|mjs|cjs))"
)

_SKIP_FRAGMENTS = (
    "node_modules", "__pycache__", "/usr/", "/home/runner",
    "dist/", ".next/", "build/", "site-packages",
)


def extract_error_files(logs: str) -> set[str]:
    """Return source files implicated by error lines (with small surrounding window)."""
    lines = logs.splitlines()
    files: set[str] = set()
    for i, line in enumerate(lines):
        if not _ERR_LINE_RE.search(line):
            continue
        # Build tools often print the file path on a neighboring line, not the error line itself.
        for j in range(max(0, i - 1), min(len(lines), i + 4)):
            for m in _FILE_IN_ERROR_RE.finditer(lines[j]):
                path = m.group(1).lstrip("./")
                if any(frag in path for frag in _SKIP_FRAGMENTS):
                    continue
                files.add(path)
    return files


def is_preexisting_failure(new_logs: str, changed_paths: set[str]) -> tuple[bool, set[str]]:
    """
    Decide whether a fix-branch CI failure is a pre-existing issue unrelated to Prash's fix.

    Pre-existing = the failure clearly implicates one or more source files, and NONE of
    those files are files that Prash modified. In that case the fix did not cause the
    failure — the repo had a separate latent breakage that only surfaced after the
    original blocker was removed.

    Returns (is_preexisting, error_files). When the failure can't be tied to any source
    file (error_files empty), returns False so the normal retry loop still runs.
    """
    error_files = extract_error_files(new_logs)
    if not error_files:
        return False, set()
    changed = {p.lstrip("./") for p in changed_paths}
    implicated = error_files & changed
    if not implicated:
        return True, error_files
    return False, error_files
