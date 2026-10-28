"""User persistence: Supabase table `users` (primary) and static accounts from secrets/users.json (fallback).

No Streamlit imports here; the UI layer builds the stores from st.secrets.
"""

from __future__ import annotations

import base64
import json
import logging
from datetime import date
from typing import Any, Protocol

from auditor.auth import User, UserConfigError, UserDirectory, normalize_email

logger = logging.getLogger(__name__)

DEFAULT_TABLE = "users"
REQUEST_TIMEOUT_S = 10

# PostgreSQL / PostgREST error codes we translate into clear messages.
_UNIQUE_VIOLATION = "23505"
_RLS_VIOLATION = "42501"
_SCHEMA_ERRORS = {"42P01", "42703", "PGRST204", "PGRST205"}  # missing table / column


def is_public_key(key: str) -> bool:
    """True for Supabase client-side keys: 'sb_publishable_...' or a legacy JWT whose role is 'anon'."""
    if key.startswith("sb_publishable_"):
        return True
    parts = key.split(".")
    if len(parts) != 3:
        return False
    try:
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except (ValueError, TypeError):
        return False
    return isinstance(payload, dict) and payload.get("role") == "anon"


class UserStoreError(Exception):
    """The store could not complete the operation. The message is safe to show to end users."""


class EmailAlreadyRegistered(UserStoreError):
    pass


class UserStore(Protocol):
    name: str

    def get_user(self, email: str) -> User | None: ...


class StaticUserStore:
    """Read-only accounts defined in st.secrets [[users]] or users.json."""

    name = "secrets"

    def __init__(self, directory: UserDirectory) -> None:
        self.directory = directory

    def get_user(self, email: str) -> User | None:
        return self.directory.get(email)

    def __len__(self) -> int:
        return len(self.directory.users)


class SupabaseUserStore:
    """Accounts stored in a Supabase (PostgREST) table.

    Use the service_role key (kept server-side in Streamlit secrets) and keep RLS enabled
    with no public policies, so the password hashes are never reachable with the anon key.
    """

    name = "supabase"

    def __init__(self, client: Any, table: str = DEFAULT_TABLE) -> None:
        self._client = client
        self._table = table

    @classmethod
    def from_credentials(cls, url: str, key: str, table: str = DEFAULT_TABLE) -> "SupabaseUserStore":
        from supabase import create_client  # imported lazily: optional at import time for tests
        from supabase.lib.client_options import SyncClientOptions

        if is_public_key(key):
            logger.error(
                "Supabase: st.secrets['supabase']['key'] es la clave pública (anon/publishable). Con RLS activo "
                "no podrá leer ni crear cuentas: usa la service_role / secret key."
            )
        try:
            client = create_client(url, key, options=SyncClientOptions(postgrest_client_timeout=REQUEST_TIMEOUT_S))
        except Exception as exc:
            logger.error("Supabase: configuración inválida: %s", exc)
            raise UserStoreError("El servicio de cuentas no está configurado correctamente.") from exc
        return cls(client, table)

    # ------------------------------------------------------------- reads
    def get_user(self, email: str) -> User | None:
        email = normalize_email(email)
        try:
            response = self._client.table(self._table).select("*").eq("email", email).limit(1).execute()
        except Exception as exc:
            raise self._translate(exc, "consultar") from exc
        rows = getattr(response, "data", None) or []
        if not rows:
            return None
        try:
            return User.from_record(rows[0])
        except UserConfigError as exc:
            logger.error("Supabase: registro inválido para %s: %s", email, exc)
            raise UserStoreError("Tu cuenta tiene un problema de configuración. Contacta a soporte.") from exc

    # ------------------------------------------------------------ writes
    def create_user(
        self,
        email: str,
        password_hash: str,
        plan_type: str,
        exp_date: date | None,
        name: str | None = None,
    ) -> User:
        record: dict[str, Any] = {
            "email": normalize_email(email),
            "password_hash": password_hash,
            "plan_type": plan_type,
            "exp_date": exp_date.isoformat() if exp_date else None,
            "is_active": True,
            "is_lifetime": plan_type == "vitalicio",
        }
        if name:
            record["name"] = name.strip()
        User.from_record(record)  # validate before sending
        try:
            response = self._client.table(self._table).insert(record).execute()
        except Exception as exc:
            raise self._translate(exc, "crear") from exc
        rows = getattr(response, "data", None) or []
        return User.from_record(rows[0] if rows else record)

    # ----------------------------------------------------------- helpers
    def _translate(self, exc: Exception, action: str) -> UserStoreError:
        code = str(getattr(exc, "code", "") or "")
        detail = getattr(exc, "message", None) or str(exc)
        if code == _UNIQUE_VIOLATION:
            return EmailAlreadyRegistered("Ya existe una cuenta con ese correo. Inicia sesión.")
        if code == _RLS_VIOLATION:
            logger.error(
                "Supabase: RLS bloqueó la operación (%s). Usa la service_role key en st.secrets['supabase']['key'].",
                detail,
            )
        elif code in _SCHEMA_ERRORS:
            logger.error("Supabase: falta la tabla o una columna (%s). Ejecuta sql/supabase_schema.sql.", detail)
        else:
            logger.error("Supabase: error al %s usuario (%s): %s", action, code or type(exc).__name__, detail)
        return UserStoreError("No pudimos conectar con el servicio de cuentas. Intenta de nuevo en unos minutos.")
