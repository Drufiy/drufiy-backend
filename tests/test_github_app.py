from app.config import settings
from app.github_app import _github_app_private_key


def test_github_app_private_key_normalizes_escaped_newlines(monkeypatch):
    escaped = "-----BEGIN PRIVATE KEY-----\\nabc123\\n-----END PRIVATE KEY-----\\n"
    monkeypatch.setattr(settings, "github_app_private_key", escaped)

    normalized = _github_app_private_key()

    assert "\\n" not in normalized
    assert normalized.startswith("-----BEGIN PRIVATE KEY-----\n")
    assert normalized.endswith("\n-----END PRIVATE KEY-----")
