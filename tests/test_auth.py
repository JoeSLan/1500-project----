from datetime import date

import pytest

from auditor.auth import (
    SessionRegistry,
    User,
    UserConfigError,
    authenticate,
    evaluate_membership,
    hash_password,
    parse_users,
    verify_password,
)
from auditor.auth import add_months, next_expiration

TODAY = date(2026, 10, 26)
FAST = 1_000  # low PBKDF2 iterations to keep tests quick


def record(**overrides):
    base = {
        "email": "Cliente@Correo.com ",
        "password_hash": hash_password("Secreta123", iterations=FAST),
        "plan_type": "mensual",
        "exp_date": "2026-11-26",
        "is_active": True,
        "is_lifetime": False,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------- passwords
def test_hash_roundtrip_and_unique_salt():
    h1, h2 = hash_password("abc12345", iterations=FAST), hash_password("abc12345", iterations=FAST)
    assert h1 != h2
    assert verify_password("abc12345", h1)
    assert not verify_password("otra", h1)


@pytest.mark.parametrize("bad", ["", "texto", "md5$1$a$b", "pbkdf2_sha256$x$y$z"])
def test_verify_rejects_malformed_hash(bad):
    assert verify_password("abc", bad) is False


# -------------------------------------------------------------------- users
def test_parse_normalizes_email_and_skips_bad_rows():
    directory = parse_users(
        [record(), record(email="dup@x.com"), record(email="dup@x.com"), record(email="malo", plan_type="semanal")],
        source="test",
    )
    assert set(directory.users) == {"cliente@correo.com", "dup@x.com"}
    assert len(directory.errors) == 2
    assert directory.get("  CLIENTE@correo.COM") is not None


@pytest.mark.parametrize(
    "overrides",
    [
        {"plan_type": "semanal"},
        {"exp_date": "26/11/2026"},
        {"password_hash": "", "password": None},
        {"email": "sin-arroba"},
        {"is_active": "quizas"},
    ],
)
def test_invalid_records_raise(overrides):
    with pytest.raises(UserConfigError):
        User.from_record(record(**overrides))


def test_toml_native_date_and_string_bools():
    user = User.from_record(record(exp_date=date(2027, 1, 31), is_active="true"))
    assert user.exp_date == date(2027, 1, 31) and user.is_active


def test_authenticate_hash_and_plain_password():
    directory = parse_users(
        [record(), {"email": "prueba@x.com", "password": "plano123", "plan_type": "vitalicio"}], source="t"
    )
    assert authenticate(directory, "cliente@correo.com", "Secreta123") is not None
    assert authenticate(directory, "cliente@correo.com", "mala") is None
    assert authenticate(directory, "prueba@x.com", "plano123") is not None
    assert authenticate(directory, "noexiste@x.com", "Secreta123") is None


# --------------------------------------------------------------- membership
@pytest.mark.parametrize(
    ("overrides", "allowed", "code", "days"),
    [
        ({"exp_date": "2026-11-26"}, True, "ACTIVA", 31),
        ({"exp_date": "2026-10-26"}, True, "ACTIVA", 0),  # last day still valid
        ({"exp_date": "2026-10-25"}, False, "VENCIDA", -1),
        ({"plan_type": "anual", "exp_date": "2027-10-26"}, True, "ACTIVA", 365),
        ({"plan_type": "vitalicio", "exp_date": ""}, True, "ACTIVA", None),
        ({"is_lifetime": True, "exp_date": "2020-01-01"}, True, "ACTIVA", None),
        ({"is_active": False, "plan_type": "vitalicio"}, False, "INACTIVA", None),
        ({"exp_date": ""}, False, "CONFIG_INVALIDA", None),
    ],
)
def test_membership_rules(overrides, allowed, code, days):
    status = evaluate_membership(User.from_record(record(**overrides)), today=TODAY)
    assert (status.allowed, status.code, status.days_remaining) == (allowed, code, days)


def test_expired_message_mentions_plan_and_date():
    status = evaluate_membership(User.from_record(record(exp_date="2026-09-30")), today=TODAY)
    assert "Mensual" in status.message and "30/09/2026" in status.message


# ------------------------------------------------------------------ sessions
def test_new_login_replaces_previous_session():
    registry = SessionRegistry()
    first, replaced_first = registry.claim("a@x.com", now=0)
    second, replaced_second = registry.claim("A@x.com", now=10)
    assert not replaced_first and replaced_second
    assert registry.validate("a@x.com", first, now=20) == "REEMPLAZADA"
    assert registry.validate("a@x.com", second, now=20) == "OK"


def test_session_expires_by_idle_and_age():
    registry = SessionRegistry(max_session_age_s=100, idle_timeout_s=30)
    token, _ = registry.claim("a@x.com", now=0)
    assert registry.validate("a@x.com", token, now=25) == "OK"
    assert registry.validate("a@x.com", token, now=60) == "EXPIRADA"  # idle > 30s
    token, _ = registry.claim("a@x.com", now=0)
    for t in (20, 40, 60, 80, 99):
        assert registry.validate("a@x.com", token, now=t) == "OK"
    assert registry.validate("a@x.com", token, now=101) == "EXPIRADA"  # age > 100s


def test_expired_session_does_not_count_as_replaced():
    registry = SessionRegistry(idle_timeout_s=30)
    registry.claim("a@x.com", now=0)
    _, replaced = registry.claim("a@x.com", now=100)
    assert not replaced


def test_release_only_with_current_token():
    registry = SessionRegistry()
    old, _ = registry.claim("a@x.com", now=0)
    new, _ = registry.claim("a@x.com", now=1)
    registry.release("a@x.com", old)  # stale tab logging out must not kill the new session
    assert registry.validate("a@x.com", new, now=2) == "OK"
    registry.release("a@x.com", new)
    assert registry.validate("a@x.com", new, now=3) == "EXPIRADA"


def test_bruteforce_lockout():
    registry = SessionRegistry(max_failed_attempts=3, lockout_s=60)
    for t in (0, 1, 2):
        assert registry.seconds_locked("a@x.com", now=t) == 0
        registry.register_failure("a@x.com", now=t)
    assert registry.seconds_locked("a@x.com", now=3) > 0
    assert registry.seconds_locked("a@x.com", now=61) == 0
    registry.register_failure("b@x.com", now=0)
    registry.clear_failures("b@x.com")
    assert registry.seconds_locked("b@x.com", now=1) == 0


# --------------------------------------------------------------- admin CLI
@pytest.mark.parametrize(
    ("start", "months", "expected"),
    [(date(2026, 1, 31), 1, date(2026, 2, 28)), (date(2026, 12, 15), 1, date(2027, 1, 15)), (date(2028, 2, 29), 12, date(2029, 2, 28))],
)
def test_add_months(start, months, expected):
    assert add_months(start, months) == expected


def test_early_renewal_keeps_remaining_days():
    assert next_expiration("mensual", date(2026, 11, 10), TODAY) == date(2026, 12, 10)
    assert next_expiration("anual", date(2026, 1, 1), TODAY) == date(2027, 10, 26)  # expired -> from today
    assert next_expiration("vitalicio", date(2026, 11, 10), TODAY) is None
