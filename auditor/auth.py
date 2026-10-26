"""Access control core: users, password hashing, memberships and single-session control.

Pure Python (no Streamlit imports) so it can be unit-tested and reused by the
admin CLI. The Streamlit layer lives in ``auditor/auth_ui.py``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Literal
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

TIMEZONE = ZoneInfo("America/Mexico_City")
PlanType = Literal["mensual", "anual", "vitalicio"]
PLAN_TYPES: tuple[str, ...] = ("mensual", "anual", "vitalicio")
PLAN_LABELS: dict[str, str] = {"mensual": "Mensual", "anual": "Anual", "vitalicio": "Vitalicio"}

HASH_ALGORITHM = "pbkdf2_sha256"
HASH_ITERATIONS = 600_000
_DUMMY_HASH: str | None = None  # lazily built; used to equalize timing for unknown emails
_PLAINTEXT_WARNED: set[str] = set()  # log the plain-password warning once per account


# ---------------------------------------------------------------- passwords
def hash_password(password: str, iterations: int = HASH_ITERATIONS) -> str:
    """Return 'pbkdf2_sha256$<iterations>$<salt_b64>$<hash_b64>'."""
    if not password:
        raise ValueError("La contraseña no puede estar vacía.")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "$".join(
        [HASH_ALGORITHM, str(iterations), base64.b64encode(salt).decode(), base64.b64encode(digest).decode()]
    )


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        algorithm, iterations, salt_b64, hash_b64 = stored_hash.split("$")
        if algorithm != HASH_ALGORITHM:
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, expected)


def _dummy_hash() -> str:
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_password(secrets.token_urlsafe(16))
    return _DUMMY_HASH


# -------------------------------------------------------------------- users
class UserConfigError(ValueError):
    """A user record in secrets/users.json is malformed."""


def normalize_email(email: str) -> str:
    return email.strip().lower()


def _parse_bool(value: Any, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {"true", "1", "si", "sí", "yes"}:
        return True
    if isinstance(value, str) and value.strip().lower() in {"false", "0", "no", ""}:
        return False
    if value is None:
        return False
    raise UserConfigError(f"'{field_name}' debe ser true/false (valor: {value!r}).")


@dataclass(frozen=True)
class User:
    email: str
    plan_type: str
    is_active: bool
    is_lifetime: bool
    exp_date: date | None
    password_hash: str | None = None
    password_plain: str | None = None  # only for quick tests; prefer password_hash
    name: str | None = None

    @property
    def lifetime(self) -> bool:
        return self.is_lifetime or self.plan_type == "vitalicio"

    def check_password(self, password: str) -> bool:
        if self.password_hash:
            return verify_password(password, self.password_hash)
        if self.password_plain is not None:
            return hmac.compare_digest(password.encode("utf-8"), self.password_plain.encode("utf-8"))
        return False

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> "User":
        email = normalize_email(str(record.get("email", "")))
        if not email or "@" not in email:
            raise UserConfigError(f"Correo inválido: {record.get('email')!r}.")

        plan_type = str(record.get("plan_type", "")).strip().lower()
        if plan_type not in PLAN_TYPES:
            raise UserConfigError(f"{email}: plan_type debe ser uno de {PLAN_TYPES} (valor: {plan_type!r}).")

        raw_exp = record.get("exp_date")
        exp_date: date | None = None
        if isinstance(raw_exp, date):  # TOML dates arrive already parsed
            exp_date = raw_exp
        elif raw_exp not in (None, ""):
            try:
                exp_date = date.fromisoformat(str(raw_exp).strip())
            except ValueError as exc:
                raise UserConfigError(f"{email}: exp_date debe tener formato YYYY-MM-DD (valor: {raw_exp!r}).") from exc

        password_hash = (record.get("password_hash") or "").strip() or None
        password_plain = record.get("password")
        password_plain = str(password_plain) if password_plain not in (None, "") else None
        if not password_hash and password_plain is None:
            raise UserConfigError(f"{email}: falta password_hash (o password para pruebas).")
        if password_plain is not None and not password_hash and email not in _PLAINTEXT_WARNED:
            _PLAINTEXT_WARNED.add(email)
            logger.warning("El usuario %s usa contraseña en texto plano; cámbiala por password_hash.", email)

        return cls(
            email=email,
            plan_type=plan_type,
            is_active=_parse_bool(record.get("is_active", True), "is_active"),
            is_lifetime=_parse_bool(record.get("is_lifetime", False), "is_lifetime"),
            exp_date=exp_date,
            password_hash=password_hash,
            password_plain=password_plain,
            name=(str(record["name"]).strip() if record.get("name") else None),
        )


@dataclass(frozen=True)
class UserDirectory:
    users: dict[str, User]
    errors: list[str]
    source: str

    def get(self, email: str) -> User | None:
        return self.users.get(normalize_email(email))


def parse_users(records: Iterable[dict[str, Any]], source: str) -> UserDirectory:
    """Parse user records. Bad rows are skipped and reported, so one typo doesn't lock everyone out."""
    users: dict[str, User] = {}
    errors: list[str] = []
    for index, record in enumerate(records, start=1):
        try:
            user = User.from_record(dict(record))
        except UserConfigError as exc:
            errors.append(f"Registro {index}: {exc}")
            continue
        if user.email in users:
            errors.append(f"Registro {index}: correo duplicado {user.email} (se ignora).")
            continue
        users[user.email] = user
    for error in errors:
        logger.error("Configuración de usuarios (%s): %s", source, error)
    return UserDirectory(users=users, errors=errors, source=source)


def read_users_json(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("users", [])
    if not isinstance(data, list):
        raise UserConfigError(f"{path.name} debe contener una lista 'users'.")
    return data


def authenticate(directory: UserDirectory, email: str, password: str) -> User | None:
    """Return the user if the password matches. Runs a hash even for unknown emails (timing-safe)."""
    user = directory.get(email)
    if user is None:
        verify_password(password, _dummy_hash())
        return None
    return user if user.check_password(password) else None


# --------------------------------------------------------------- membership
@dataclass(frozen=True)
class MembershipStatus:
    allowed: bool
    code: Literal["ACTIVA", "VENCIDA", "INACTIVA", "CONFIG_INVALIDA"]
    days_remaining: int | None  # None for lifetime / not applicable
    message: str


def today_mx() -> date:
    return datetime.now(TIMEZONE).date()


def evaluate_membership(user: User, today: date | None = None) -> MembershipStatus:
    """exp_date is inclusive: access lasts through the end of that day (Mexico City time)."""
    today = today or today_mx()
    if not user.is_active:
        return MembershipStatus(False, "INACTIVA", None, "Tu cuenta está desactivada.")
    if user.lifetime:
        return MembershipStatus(True, "ACTIVA", None, "Acceso Vitalicio")
    if user.exp_date is None:
        return MembershipStatus(
            False, "CONFIG_INVALIDA", None, "Tu plan no tiene fecha de vencimiento configurada."
        )
    days = (user.exp_date - today).days
    if days < 0:
        return MembershipStatus(
            False, "VENCIDA", days, f"Tu plan {PLAN_LABELS[user.plan_type]} venció el {user.exp_date:%d/%m/%Y}."
        )
    return MembershipStatus(True, "ACTIVA", days, f"Vence el {user.exp_date:%d/%m/%Y}")


# ------------------------------------------------------------ session control
@dataclass
class _ActiveSession:
    token: str
    issued_at: float
    last_seen: float


class SessionRegistry:
    """In-process registry of the single active session token per account.

    A new login replaces the previous token ("last login wins"); the old browser
    session is closed on its next interaction. Also rate-limits failed logins.
    """

    def __init__(
        self,
        max_session_age_s: int = 12 * 3600,
        idle_timeout_s: int = 2 * 3600,
        max_failed_attempts: int = 5,
        lockout_s: int = 300,
    ) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, _ActiveSession] = {}
        self._failures: dict[str, list[float]] = {}
        self.max_session_age_s = max_session_age_s
        self.idle_timeout_s = idle_timeout_s
        self.max_failed_attempts = max_failed_attempts
        self.lockout_s = lockout_s

    # sessions
    def claim(self, email: str, now: float | None = None) -> tuple[str, bool]:
        """Issue a new token for this account. Returns (token, replaced_existing_session)."""
        now = time.time() if now is None else now
        email = normalize_email(email)
        token = secrets.token_urlsafe(32)
        with self._lock:
            previous = self._sessions.get(email)
            replaced = previous is not None and not self._expired(previous, now)
            self._sessions[email] = _ActiveSession(token=token, issued_at=now, last_seen=now)
        return token, replaced

    def validate(self, email: str, token: str, now: float | None = None) -> Literal["OK", "REEMPLAZADA", "EXPIRADA"]:
        now = time.time() if now is None else now
        email = normalize_email(email)
        with self._lock:
            current = self._sessions.get(email)
            if current is None:
                return "EXPIRADA"
            if not hmac.compare_digest(current.token, token):
                return "REEMPLAZADA"
            if self._expired(current, now):
                del self._sessions[email]
                return "EXPIRADA"
            current.last_seen = now
            return "OK"

    def release(self, email: str, token: str) -> None:
        email = normalize_email(email)
        with self._lock:
            current = self._sessions.get(email)
            if current is not None and hmac.compare_digest(current.token, token):
                del self._sessions[email]

    def active_count(self) -> int:
        with self._lock:
            return len(self._sessions)

    def _expired(self, session: _ActiveSession, now: float) -> bool:
        return now - session.issued_at > self.max_session_age_s or now - session.last_seen > self.idle_timeout_s

    # brute-force protection
    def seconds_locked(self, email: str, now: float | None = None) -> int:
        now = time.time() if now is None else now
        with self._lock:
            recent = [t for t in self._failures.get(normalize_email(email), []) if now - t < self.lockout_s]
            self._failures[normalize_email(email)] = recent
            if len(recent) < self.max_failed_attempts:
                return 0
            return int(self.lockout_s - (now - recent[0])) + 1

    def register_failure(self, email: str, now: float | None = None) -> None:
        with self._lock:
            self._failures.setdefault(normalize_email(email), []).append(time.time() if now is None else now)

    def clear_failures(self, email: str) -> None:
        with self._lock:
            self._failures.pop(normalize_email(email), None)
