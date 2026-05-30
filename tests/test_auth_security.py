import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from jose import jwt

from app.auth import create_access_token, get_current_user, revoke_access_token
from app.config import settings
from app.routes.github_oauth import _create_oauth_state, _validate_oauth_state


class _Table:
    def __init__(self, parent, name):
        self.parent = parent
        self.name = name
        self.row = None

    def select(self, *_args):
        return self

    def eq(self, *_args):
        return self

    def limit(self, *_args):
        return self

    def single(self):
        return self

    def upsert(self, row, on_conflict=None):
        self.row = row
        self.parent.upserts.append((self.name, row, on_conflict))
        return self

    def execute(self):
        if self.name == "jwt_revocations":
            return type("Result", (), {"data": self.parent.revoked_rows})()
        if self.name == "user_profiles":
            return type("Result", (), {"data": self.parent.user_row})()
        return type("Result", (), {"data": []})()


class _Supabase:
    def __init__(self, revoked_rows=None, user_row=None):
        self.revoked_rows = revoked_rows or []
        self.user_row = user_row or {
            "id": "user-1",
            "github_username": "octo",
            "email": "octo@example.com",
        }
        self.upserts = []

    def table(self, name):
        return _Table(self, name)


def _decode(token):
    return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])


def test_access_token_contains_revocable_jti():
    token = create_access_token("user-1", "octo")
    payload = _decode(token)

    assert payload["sub"] == "user-1"
    assert payload["github_username"] == "octo"
    assert isinstance(payload["jti"], str)
    assert len(payload["jti"]) >= 16


def test_revoke_access_token_writes_jti(monkeypatch):
    fake_db = _Supabase()
    monkeypatch.setattr("app.auth.supabase", fake_db)
    monkeypatch.setattr("app.auth._revocation_table_available", None)

    token = create_access_token("user-1", "octo")
    assert revoke_access_token(token, user_id="user-1") is True

    table_name, row, conflict = fake_db.upserts[0]
    assert table_name == "jwt_revocations"
    assert row["jti"] == _decode(token)["jti"]
    assert row["user_id"] == "user-1"
    assert row["expires_at"]
    assert conflict == "jti"


@pytest.mark.asyncio
async def test_revoked_token_is_rejected(monkeypatch):
    token = create_access_token("user-1", "octo")
    fake_db = _Supabase(revoked_rows=[{"jti": _decode(token)["jti"]}])
    monkeypatch.setattr("app.auth.supabase", fake_db)
    monkeypatch.setattr("app.auth._revocation_table_available", None)

    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
    with pytest.raises(HTTPException) as exc:
        await get_current_user(credentials)

    assert exc.value.status_code == 401
    assert exc.value.detail == "Token has been revoked"


def test_oauth_state_roundtrip():
    state = _create_oauth_state()
    _validate_oauth_state(state)


def test_oauth_state_rejects_invalid_token():
    with pytest.raises(HTTPException) as exc:
        _validate_oauth_state("not-a-real-state-token")

    assert exc.value.status_code == 400
    assert exc.value.detail["error"] == "invalid_oauth_state"
