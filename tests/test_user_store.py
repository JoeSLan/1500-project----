from datetime import date

import httpx
import pytest
from postgrest.exceptions import APIError

from auditor.auth import RateLimiter, hash_password, signup_expiration, validate_registration, verify_password
from auditor.user_store import EmailAlreadyRegistered, SupabaseUserStore, UserStoreError
from tests.fake_supabase import FakeSupabase


@pytest.fixture
def db():
    return FakeSupabase()


@pytest.fixture
def store(db):
    return SupabaseUserStore(db, table="users")


def test_create_and_get_user(store, db):
    created = store.create_user(
        email="  Nuevo@Correo.MX ", password_hash=hash_password("Secreta123", iterations=1_000),
        plan_type="mensual", exp_date=date(2026, 11, 28),
    )
    assert created.email == "nuevo@correo.mx" and created.exp_date == date(2026, 11, 28)
    row = db.rows()[0]
    assert row["email"] == "nuevo@correo.mx"
    assert row["exp_date"] == "2026-11-28" and row["is_active"] is True and row["is_lifetime"] is False
    assert row["password_hash"].startswith("pbkdf2_sha256$") and "Secreta123" not in str(row)

    fetched = store.get_user("NUEVO@correo.mx")
    assert fetched is not None and verify_password("Secreta123", fetched.password_hash)
    assert store.get_user("otro@correo.mx") is None


def test_duplicate_email(store):
    kwargs = dict(password_hash=hash_password("x" * 8, iterations=1_000), plan_type="mensual", exp_date=date(2026, 11, 28))
    store.create_user(email="a@b.mx", **kwargs)
    with pytest.raises(EmailAlreadyRegistered):
        store.create_user(email="A@B.mx", **kwargs)


def test_rows_from_existing_tables_with_extra_or_missing_columns(store, db):
    db.tables["users"] = [{
        "id": 7, "email": "viejo@x.mx", "password_hash": hash_password("abc12345", iterations=1_000),
        "plan_type": "anual", "exp_date": "2027-01-31", "is_active": True, "session_token": "legacy",
    }]
    user = store.get_user("viejo@x.mx")
    assert user is not None and user.plan_type == "anual" and user.is_lifetime is False


def test_malformed_row_raises_friendly_error(store, db):
    db.tables["users"] = [{"email": "malo@x.mx", "password_hash": "h", "plan_type": None}]
    with pytest.raises(UserStoreError, match="configuración"):
        store.get_user("malo@x.mx")


@pytest.mark.parametrize(
    "error",
    [
        APIError({"code": "42501", "message": "new row violates row-level security policy"}),
        APIError({"code": "PGRST205", "message": "Could not find the table 'public.users'"}),
        httpx.ConnectError("sin red"),
    ],
)
def test_backend_errors_become_user_store_errors(store, db, error):
    db.fail_with = error
    with pytest.raises(UserStoreError) as info:
        store.get_user("a@b.mx")
    assert not isinstance(info.value, EmailAlreadyRegistered)
    assert "servicio de cuentas" in str(info.value)


def test_from_credentials_builds_real_client():
    store = SupabaseUserStore.from_credentials("https://abcdefghijklmnop.supabase.co", "sb_secret_test")
    assert store.name == "supabase"


@pytest.mark.parametrize(
    ("email", "password", "confirm", "expected"),
    [
        ("ok@correo.mx", "12345678", "12345678", None),
        ("sin-arroba", "12345678", "12345678", "correo"),
        ("a@b", "12345678", "12345678", "correo"),
        ("ok@correo.mx", "1234567", "1234567", "8 caracteres"),
        ("ok@correo.mx", "12345678", "12345679", "no coinciden"),
    ],
)
def test_validate_registration(email, password, confirm, expected):
    result = validate_registration(email, password, confirm)
    assert (result is None) if expected is None else (expected in result)


def test_signup_expiration():
    today = date(2026, 1, 31)
    assert signup_expiration(today, None) == date(2026, 2, 28)  # one calendar month
    assert signup_expiration(today, 7) == date(2026, 2, 7)
    assert signup_expiration(today, -1) == date(2026, 1, 30)  # pay before access


def test_rate_limiter():
    limiter = RateLimiter()
    assert all(limiter.allow("k", 2, 60, now=t) for t in (0, 1))
    assert not limiter.allow("k", 2, 60, now=2)
    assert limiter.allow("k", 2, 60, now=61)
    assert limiter.allow("otra", 2, 60, now=2)


def _jwt(payload: dict) -> str:
    import base64
    import json

    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()  # noqa: E731
    return f"{enc({'alg': 'HS256'})}.{enc(payload)}.firma"


@pytest.mark.parametrize(
    ("key", "public"),
    [
        ("sb_publishable_abc", True),
        ("sb_secret_abc", False),
        (_jwt({"role": "anon"}), True),
        (_jwt({"role": "service_role"}), False),
        ("no-es-una-clave", False),
    ],
)
def test_detects_public_keys(key, public):
    from auditor.user_store import is_public_key

    assert is_public_key(key) is public
