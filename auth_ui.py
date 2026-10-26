"""Streamlit layer for access control: login screen, paywall, single-session check and account panel.

Usage in app.py:
    account = require_auth()        # blocks with st.stop() until the user may use the app
    render_account_panel(account)   # sidebar: email, plan, days left, logout
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import streamlit as st

from auditor.auth import (
    PLAN_LABELS,
    MembershipStatus,
    SessionRegistry,
    User,
    UserConfigError,
    UserDirectory,
    authenticate,
    evaluate_membership,
    parse_users,
    read_users_json,
)
from auditor.config import PROJECT_ROOT

logger = logging.getLogger(__name__)

DEFAULT_USERS_JSON = PROJECT_ROOT / "users.json"
EXPIRY_WARNING_DAYS = 7

_EMAIL = "auth_email"
_TOKEN = "auth_token"
_NOTICE = "auth_notice"


@dataclass(frozen=True)
class Account:
    user: User
    status: MembershipStatus


# ------------------------------------------------------------- configuration
def _secrets_section(name: str) -> Any:
    """Read a top-level st.secrets section; returns None when there is no secrets file or key."""
    try:
        return st.secrets[name] if name in st.secrets else None
    except Exception:  # no secrets.toml locally -> Streamlit raises
        return None


def _to_plain(value: Any) -> Any:
    """Convert Streamlit's read-only AttrDict/lists into plain Python containers."""
    if hasattr(value, "items"):
        return {k: _to_plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_plain(v) for v in value]
    return value


def load_user_directory() -> UserDirectory:
    """Users come from st.secrets ([[users]] tables) or, if absent, from a local users.json.

    Read on every rerun so deactivations or renewals made by the admin apply immediately.
    """
    secret_users = _secrets_section("users")
    if secret_users:
        return parse_users(_to_plain(secret_users), source="st.secrets")
    path = Path(os.getenv("LEGALSITE_USERS_FILE") or DEFAULT_USERS_JSON)
    try:
        return parse_users(read_users_json(path), source=path.name)
    except (UserConfigError, ValueError) as exc:  # ValueError covers invalid JSON
        logger.error("No se pudo leer %s: %s", path.name, exc)
        return UserDirectory(users={}, errors=[str(exc)], source=path.name)


def _saas_setting(key: str, env_var: str) -> str | None:
    section = _secrets_section("saas")
    if section and section.get(key):
        return str(section.get(key))
    return os.getenv(env_var) or None


@st.cache_resource
def get_session_registry() -> SessionRegistry:
    """One registry per server process, shared by every browser session."""
    return SessionRegistry()


# ------------------------------------------------------------- session state
def _set_notice(kind: str, text: str) -> None:
    st.session_state[_NOTICE] = (kind, text)


def _show_notice() -> None:
    notice = st.session_state.pop(_NOTICE, None)
    if notice:
        kind, text = notice
        {"info": st.info, "warning": st.warning, "success": st.success}.get(kind, st.info)(text)


def _end_session(notice: tuple[str, str] | None = None) -> None:
    """Wipe everything in this browser session (auth + analysis results)."""
    st.session_state.clear()
    if notice:
        _set_notice(*notice)


def logout() -> None:
    email, token = st.session_state.get(_EMAIL), st.session_state.get(_TOKEN)
    if email and token:
        get_session_registry().release(email, token)
    _end_session(("success", "Cerraste sesión correctamente."))


# ------------------------------------------------------------------- screens
def _render_header() -> None:
    st.title("⚖️ LegalSite Auditor")
    st.caption("Due diligence legal-urbana asistida por IA para inmuebles en el Estado de México y CDMX.")


def render_paywall(user: User, status: MembershipStatus) -> None:
    if status.code == "VENCIDA":
        st.error(f"⏳ {status.message}")
        st.markdown("Renueva tu suscripción para seguir auditando documentos con LegalSite Auditor.")
    elif status.code == "INACTIVA":
        st.error("🚫 Tu cuenta no está activa.")
        st.markdown("Si crees que es un error o deseas reactivarla, contáctanos.")
    else:
        st.error("⚠️ Hay un problema con la configuración de tu plan.")
        st.markdown("Contáctanos para resolverlo; tu acceso se restablecerá en cuanto lo corrijamos.")

    renewal_url = _saas_setting("renewal_url", "LEGALSITE_RENEWAL_URL")
    support_email = _saas_setting("support_email", "LEGALSITE_SUPPORT_EMAIL")
    if renewal_url:
        st.link_button("💳 Renovar mi plan", renewal_url, type="primary")
    if support_email:
        st.caption(f"Soporte: {support_email}")
    st.caption(f"Cuenta: {user.email} · Plan {PLAN_LABELS[user.plan_type]}")


def _render_login_form(directory: UserDirectory) -> None:
    registry = get_session_registry()
    _, center, _ = st.columns([1, 2, 1])
    with center:
        _render_header()
        _show_notice()

        if not directory.users:
            st.error("El servicio no está disponible: no hay cuentas configuradas. Contacta al administrador.")
            return

        with st.form("login", clear_on_submit=False):
            st.subheader("Iniciar sesión")
            email = st.text_input("Correo electrónico", autocomplete="email")
            password = st.text_input("Contraseña", type="password", autocomplete="current-password")
            submitted = st.form_submit_button("Entrar", type="primary", width="stretch")

        if not submitted:
            return
        if not email.strip() or not password:
            st.warning("Escribe tu correo y contraseña.")
            return

        locked_for = registry.seconds_locked(email)
        if locked_for:
            st.error(f"Demasiados intentos fallidos. Intenta de nuevo en {locked_for // 60 + 1} minuto(s).")
            return

        user = authenticate(directory, email, password)
        if user is None:
            registry.register_failure(email)
            st.error("Correo o contraseña incorrectos.")
            return
        registry.clear_failures(email)

        status = evaluate_membership(user)
        if not status.allowed:
            render_paywall(user, status)
            return

        token, replaced = registry.claim(user.email)
        _end_session()  # drop anything left from a previous user in this browser
        st.session_state[_EMAIL] = user.email
        st.session_state[_TOKEN] = token
        if replaced:
            _set_notice("warning", "Había otra sesión abierta con esta cuenta; se cerró automáticamente.")
        logger.info("Login: %s (plan %s)", user.email, user.plan_type)
        st.rerun()


# --------------------------------------------------------------------- gate
def require_auth() -> Account:
    """Return the authenticated account, or render login/paywall and stop the script."""
    directory = load_user_directory()
    email, token = st.session_state.get(_EMAIL), st.session_state.get(_TOKEN)

    if not (email and token):
        _render_login_form(directory)
        st.stop()

    verdict = get_session_registry().validate(email, token)
    if verdict == "REEMPLAZADA":
        _end_session(("warning", "Tu sesión se cerró porque esta cuenta se abrió en otro dispositivo o navegador."))
        st.rerun()
    if verdict == "EXPIRADA":
        _end_session(("info", "Tu sesión expiró. Vuelve a iniciar sesión."))
        st.rerun()

    user = directory.get(email)
    if user is None:
        get_session_registry().release(email, token)
        _end_session(("warning", "Tu cuenta ya no está disponible. Contacta a soporte."))
        st.rerun()

    status = evaluate_membership(user)
    if not status.allowed:
        # Membership ended while logged in (expired at midnight, or admin deactivated it).
        _render_header()
        render_paywall(user, status)
        if st.button("Cerrar sesión"):
            logout()
            st.rerun()
        st.stop()

    _show_notice()  # e.g. "another session with this account was closed"
    return Account(user=user, status=status)


# ------------------------------------------------------------------ sidebar
def render_account_panel(account: Account) -> None:
    user, status = account.user, account.status
    with st.sidebar:
        st.header("👤 Mi cuenta")
        if user.name:
            st.markdown(f"**{user.name}**")
        st.markdown(f"📧 {user.email}")
        st.markdown(f"🏷️ Plan **{PLAN_LABELS[user.plan_type]}**")

        if status.days_remaining is None:
            st.success("♾️ Acceso Vitalicio")
        elif status.days_remaining == 0:
            st.warning("⏳ Tu plan vence hoy")
        else:
            label = "día restante" if status.days_remaining == 1 else "días restantes"
            message = f"📅 {status.days_remaining} {label} · {status.message.lower()}"
            if status.days_remaining <= EXPIRY_WARNING_DAYS:
                st.warning(message)
            else:
                st.info(message)

        if status.days_remaining is not None and status.days_remaining <= EXPIRY_WARNING_DAYS:
            renewal_url = _saas_setting("renewal_url", "LEGALSITE_RENEWAL_URL")
            if renewal_url:
                st.link_button("💳 Renovar ahora", renewal_url, width="stretch")

        if st.button("🚪 Cerrar Sesión", width="stretch"):
            logout()
            st.rerun()
        st.divider()
