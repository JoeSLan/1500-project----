"""Streamlit layer for access control: login / sign-up, subscription plans, single session, account panel.

Accounts live in the Supabase table `users` (st.secrets["supabase"]["url"/"key"]). Accounts in
st.secrets [[users]] or users.json keep working as a read-only fallback (e.g. admin/demo accounts).

Usage in app.py:
    account = require_auth()        # blocks with st.stop() until the user may use the app
    render_account_panel(account)   # sidebar: email, plan, days left, logout
"""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import streamlit as st

from auditor.auth import (
    PLAN_LABELS,
    PLAN_TYPES,
    MembershipStatus,
    RateLimiter,
    SessionRegistry,
    User,
    UserConfigError,
    UserDirectory,
    check_credentials,
    evaluate_membership,
    hash_password,
    parse_users,
    read_users_json,
    signup_expiration,
    today_mx,
    validate_registration,
)
from auditor.config import PROJECT_ROOT
from auditor.payments import (
    PaymentConfigError,
    StripeConfig,
    checkout_url,
    fulfill_checkout_session,
    is_session_id,
    links_mode,
    mask_email,
    merge_plan_links,
    validate_secret_key,
)
from auditor.user_store import (
    DEFAULT_TABLE,
    EmailAlreadyRegistered,
    StaticUserStore,
    SupabaseUserStore,
    UserStoreError,
)

logger = logging.getLogger(__name__)

DEFAULT_USERS_JSON = PROJECT_ROOT / "users.json"
EXPIRY_WARNING_DAYS = 7
DEFAULT_SIGNUP_PLAN = "mensual"
DEFAULT_TRIAL_DAYS = 3  # free trial on sign-up; override with [saas] signup_days

# Sign-up throttling (per browser session and per server process).
SIGNUPS_PER_SESSION = 3
SIGNUPS_PER_SESSION_WINDOW_S = 600
SIGNUPS_GLOBAL = 30
SIGNUPS_GLOBAL_WINDOW_S = 3600

_EMAIL = "auth_email"
_TOKEN = "auth_token"
_SOURCE = "auth_source"
_NOTICE = "auth_notice"
_BROWSER_ID = "auth_browser_id"
_HANDLED_SESSIONS = "payments_handled_sessions"

PLAN_CATALOG: dict[str, dict[str, Any]] = {
    "mensual": {
        "title": "Mensual",
        "tagline": "Acceso completo, renovación cada mes.",
        "period": "/ mes",
        "features": ["Auditoría de PDFs legales y PDU", "Reporte ejecutivo en Word, Markdown y JSON"],
    },
    "anual": {
        "title": "Anual",
        "tagline": "12 meses de acceso con un solo pago.",
        "period": "/ año",
        "features": ["Todo lo del plan Mensual", "Un pago al año"],
    },
    "vitalicio": {
        "title": "De por vida (Lifetime)",
        "tagline": "Pago único, acceso permanente.",
        "period": "pago único",
        "features": ["Todo lo del plan Anual", "Sin renovaciones"],
    },
}


@dataclass(frozen=True)
class Account:
    user: User
    status: MembershipStatus
    source: str  # "supabase" or "secrets"


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


def _saas_config() -> dict[str, Any]:
    section = _secrets_section("saas")
    return _to_plain(section) if section else {}


def _saas_setting(key: str, env_var: str) -> str | None:
    value = _saas_config().get(key)
    if value:
        return str(value)
    return os.getenv(env_var) or None


def _signup_days() -> int:
    """[saas] signup_days: free-trial days granted on sign-up. Unset -> DEFAULT_TRIAL_DAYS."""
    raw = _saas_config().get("signup_days")
    if raw in (None, ""):
        return DEFAULT_TRIAL_DAYS
    try:
        return int(raw)
    except (TypeError, ValueError):
        logger.error("[saas] signup_days debe ser un entero (valor: %r); se usan %s días.", raw, DEFAULT_TRIAL_DAYS)
        return DEFAULT_TRIAL_DAYS


def load_user_directory() -> UserDirectory:
    """Static accounts from st.secrets ([[users]] tables) or, if absent, from a local users.json.

    Read on every rerun so changes made by the admin apply immediately.
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


@st.cache_resource(show_spinner=False)
def _build_supabase_store(url: str, key: str, table: str) -> SupabaseUserStore | None:
    try:
        return SupabaseUserStore.from_credentials(url, key, table)
    except UserStoreError:
        return None


def get_supabase_store() -> SupabaseUserStore | None:
    section = _secrets_section("supabase")
    if not section:
        return None
    config = _to_plain(section)
    url, key = str(config.get("url") or "").strip(), str(config.get("key") or "").strip()
    if not (url and key):
        logger.error("st.secrets['supabase'] debe tener 'url' y 'key'.")
        return None
    return _build_supabase_store(url, key, str(config.get("table") or DEFAULT_TABLE))


def plan_links() -> dict[str, str]:
    """Payment Link per plan: [saas.plans.<plan>].url overrides the built-in Stripe links."""
    return merge_plan_links(_saas_config().get("plans"))


def price_plans() -> dict[str, str]:
    """Optional [stripe.price_plans] {"price_...": "mensual"} mapping (alternative to link matching)."""
    section = _secrets_section("stripe")
    mapping = (_to_plain(section) or {}).get("price_plans") if section else None
    return {str(k): str(v) for k, v in (mapping or {}).items()}


def get_stripe_config() -> StripeConfig | None:
    """Validated st.secrets["stripe"]["secret_key"], or None (logged) when missing/invalid."""
    section = _secrets_section("stripe")
    if not section:
        return None
    try:
        config = validate_secret_key(_to_plain(section).get("secret_key"))
    except PaymentConfigError as exc:
        logger.error("Stripe: %s", exc)
        return None
    mode = links_mode(plan_links())
    if mode and mode != config.mode:
        logger.error(
            "Stripe: la secret_key es de modo %s pero los Payment Links son de modo %s; los pagos no se "
            "podrán verificar desde la app.", config.mode, mode,
        )
    return config


@st.cache_resource(show_spinner=False)
def _build_stripe_client(secret_key: str) -> Any:
    import stripe  # lazy: only needed when Stripe is configured

    options: dict[str, Any] = {"max_network_retries": 2}
    api_base = os.getenv("STRIPE_API_BASE")  # only for local tests
    if api_base:
        options["base_addresses"] = {"api": api_base}
    return stripe.StripeClient(secret_key, **options)


def get_stripe_client() -> Any | None:
    config = get_stripe_config()
    return _build_stripe_client(config.secret_key) if config else None


@st.cache_resource
def get_session_registry() -> SessionRegistry:
    """One registry per server process, shared by every browser session."""
    return SessionRegistry()


@st.cache_resource
def get_signup_limiter() -> RateLimiter:
    return RateLimiter()


# ------------------------------------------------------------- session state
def _set_notice(kind: str, text: str) -> None:
    st.session_state[_NOTICE] = (kind, text)


def _show_notice() -> None:
    notice = st.session_state.pop(_NOTICE, None)
    if notice:
        kind, text = notice
        {"info": st.info, "warning": st.warning, "success": st.success}.get(kind, st.info)(text)


def _browser_id() -> str:
    if _BROWSER_ID not in st.session_state:
        st.session_state[_BROWSER_ID] = uuid.uuid4().hex
    return st.session_state[_BROWSER_ID]


def _end_session(notice: tuple[str, str] | None = None) -> None:
    """Wipe everything in this browser session (auth + analysis results), keeping the browser id."""
    browser_id = st.session_state.get(_BROWSER_ID)
    st.session_state.clear()
    if browser_id:
        st.session_state[_BROWSER_ID] = browser_id
    if notice:
        _set_notice(*notice)


def _start_session(user: User, source: str) -> bool:
    """Issue the single active token for this account. Returns True if another session was closed."""
    token, replaced = get_session_registry().claim(user.email)
    _end_session()  # drop anything left from a previous user in this browser
    st.session_state[_EMAIL] = user.email
    st.session_state[_TOKEN] = token
    st.session_state[_SOURCE] = source
    logger.info("Login: %s (plan %s, %s)", user.email, user.plan_type, source)
    return replaced


def logout() -> None:
    email, token = st.session_state.get(_EMAIL), st.session_state.get(_TOKEN)
    if email and token:
        get_session_registry().release(email, token)
    _end_session(("success", "Cerraste sesión correctamente."))


# --------------------------------------------------------------------- plans
def _plan_url(plan: str, email: str | None) -> str | None:
    """Payment link for a plan, personalized with the account email (prefilled_email +
    client_reference_id) so the webhook can credit the right account."""
    url = plan_links().get(plan)
    return checkout_url(url, email) if url else None


def _plan_price(plan: str) -> str | None:
    plans = _saas_config().get("plans") or {}
    price = (plans.get(plan) or {}).get("price")
    return str(price) if price else None


def render_plans(user: User | None = None, heading: str = "Planes de suscripción") -> None:
    st.subheader(heading)
    support_email = _saas_setting("support_email", "LEGALSITE_SUPPORT_EMAIL")
    columns = st.columns(len(PLAN_TYPES))
    for column, plan in zip(columns, PLAN_TYPES):
        info = PLAN_CATALOG[plan]
        is_current = user is not None and user.plan_type == plan
        with column, st.container(border=True):
            st.markdown(f"#### {info['title']}")
            if is_current:
                st.caption("⭐ Tu plan actual")
            price = _plan_price(plan)
            if price:
                st.markdown(f"### {price} <small>{info['period']}</small>", unsafe_allow_html=True)
            st.caption(info["tagline"])
            st.markdown("\n".join(f"- {feature}" for feature in info["features"]))

            url = _plan_url(plan, user.email if user else None)
            if user is None:
                label = "Elegir plan"
            elif is_current:
                label = "Renovar"
            else:
                label = "Cambiar a este plan"
            if url:
                st.link_button(f"💳 {label}", url, type="primary" if is_current else "secondary", width="stretch")
            else:
                st.button(f"💳 {label}", key=f"plan_{plan}", disabled=True, width="stretch")
                st.caption("Pago en línea próximamente.")
    if user is None:
        st.caption(
            "Crea tu cuenta en **Registrarse** y elige tu plan desde ahí. Si pagas antes, regístrate con el "
            "mismo correo que usaste al pagar y tu plan se activará solo."
        )
    st.caption(
        "Tu acceso se activa o extiende automáticamente al confirmarse el pago."
        + (f" Dudas: {support_email}" if support_email else "")
    )
    if links_mode(plan_links()) == "test":
        st.caption("🧪 Pagos en modo de prueba: usa la tarjeta 4242 4242 4242 4242, cualquier fecha futura y CVC.")


def render_paywall(user: User, status: MembershipStatus) -> None:
    support_email = _saas_setting("support_email", "LEGALSITE_SUPPORT_EMAIL")
    if status.code == "VENCIDA":
        st.error(f"⏳ {status.message}")
        st.markdown("Renueva o elige un plan para seguir auditando documentos con LegalSite Auditor.")
        render_plans(user, heading="Elige tu plan")
    elif status.code == "INACTIVA":
        st.error("🚫 Tu cuenta no está activa.")
        st.markdown("Si crees que es un error o deseas reactivarla, contáctanos.")
        if support_email:
            st.caption(f"Soporte: {support_email}")
    else:
        st.error("⚠️ Hay un problema con la configuración de tu plan.")
        st.markdown("Contáctanos para resolverlo; tu acceso se restablecerá en cuanto lo corrijamos.")
        if support_email:
            st.caption(f"Soporte: {support_email}")
    st.caption(f"Cuenta: {user.email} · Plan {PLAN_LABELS[user.plan_type]}")


# ------------------------------------------------------------------- screens
def _render_header() -> None:
    st.title("⚖️ LegalSite Auditor")
    st.caption("Due diligence legal-urbana asistida por IA para inmuebles en el Estado de México y CDMX.")


def _find_user(email: str, supabase: SupabaseUserStore | None, static: StaticUserStore) -> tuple[User | None, str | None]:
    """Supabase first; static accounts only if the email isn't in Supabase. Raises UserStoreError."""
    if supabase is not None:
        try:
            user = supabase.get_user(email)
        except UserStoreError:
            user = static.get_user(email)
            if user is None:
                raise
            return user, static.name
        if user is not None:
            return user, supabase.name
    user = static.get_user(email)
    return (user, static.name) if user else (None, None)


def _render_login_tab(supabase: SupabaseUserStore | None, static: StaticUserStore) -> None:
    registry = get_session_registry()
    with st.form("login", clear_on_submit=False):
        email = st.text_input("Correo electrónico", key="login_email", autocomplete="email")
        password = st.text_input("Contraseña", type="password", key="login_password", autocomplete="current-password")
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

    try:
        candidate, source = _find_user(email, supabase, static)
    except UserStoreError as exc:
        st.error(str(exc))
        return

    user = check_credentials(candidate, password)
    if user is None or source is None:
        registry.register_failure(email)
        st.error("Correo o contraseña incorrectos.")
        return
    registry.clear_failures(email)

    replaced = _start_session(user, source)
    if replaced:
        _set_notice("warning", "Había otra sesión abierta con esta cuenta; se cerró automáticamente.")
    st.rerun()


def _render_signup_tab(supabase: SupabaseUserStore | None, static: StaticUserStore) -> None:
    if supabase is None:
        st.info("El registro en línea no está disponible por ahora. Escríbenos para crear tu cuenta.")
        support_email = _saas_setting("support_email", "LEGALSITE_SUPPORT_EMAIL")
        if support_email:
            st.caption(f"Contacto: {support_email}")
        return

    with st.form("signup", clear_on_submit=False):
        email = st.text_input("Correo electrónico", key="signup_email", autocomplete="email")
        password = st.text_input(
            "Contraseña (mínimo 8 caracteres)", type="password", key="signup_password", autocomplete="new-password"
        )
        confirm = st.text_input("Confirma tu contraseña", type="password", key="signup_confirm", autocomplete="new-password")
        submitted = st.form_submit_button("Crear cuenta", type="primary", width="stretch")

    if not submitted:
        return
    error = validate_registration(email, password, confirm)
    if error:
        st.warning(error)
        return
    if static.get_user(email) is not None:
        st.error("Ya existe una cuenta con ese correo. Inicia sesión.")
        return

    limiter = get_signup_limiter()
    if not (
        limiter.allow(f"session:{_browser_id()}", SIGNUPS_PER_SESSION, SIGNUPS_PER_SESSION_WINDOW_S)
        and limiter.allow("global", SIGNUPS_GLOBAL, SIGNUPS_GLOBAL_WINDOW_S)
    ):
        st.error("Se alcanzó el límite de registros por ahora. Intenta de nuevo más tarde.")
        return

    try:
        user = supabase.create_user(
            email=email,
            password_hash=hash_password(password),
            plan_type=DEFAULT_SIGNUP_PLAN,
            exp_date=signup_expiration(today_mx(), _signup_days()),
        )
    except EmailAlreadyRegistered as exc:
        st.error(str(exc))
        return
    except UserStoreError as exc:
        st.error(str(exc))
        return

    logger.info("Registro: %s (vence %s)", user.email, user.exp_date)
    _start_session(user, supabase.name)
    status = evaluate_membership(user)
    if status.allowed:
        _set_notice(
            "success",
            f"¡Bienvenido! Tu prueba gratis está activa: {status.message.lower()}. "
            "Elige un plan antes de que termine para no perder el acceso.",
        )
    st.rerun()


def _render_access_screen(supabase: SupabaseUserStore | None, static: StaticUserStore) -> None:
    _, center, _ = st.columns([1, 2, 1])
    with center:
        _render_header()
        _show_notice()
        if supabase is None and len(static) == 0:
            st.error("El servicio no está disponible: no hay cuentas configuradas. Contacta al administrador.")
            return
        tab_login, tab_signup = st.tabs(["Iniciar sesión", "Registrarse"])
        with tab_login:
            _render_login_tab(supabase, static)
        with tab_signup:
            _render_signup_tab(supabase, static)
    st.divider()
    render_plans()


def _render_retry(message: str) -> None:
    _render_header()
    st.error(message)
    left, right = st.columns(2)
    if left.button("🔄 Reintentar", width="stretch"):
        st.rerun()
    if right.button("Cerrar sesión", width="stretch"):
        logout()
        st.rerun()


# ------------------------------------------------------- return from Stripe
def _plan_title(plan: str | None) -> str:
    return PLAN_CATALOG.get(plan or "", {}).get("title", "tu plan")


def handle_payment_return(supabase: SupabaseUserStore | None) -> None:
    """Fulfill ?session_id=cs_... when the buyer is redirected back from a Stripe Payment Link.

    The webhook does the same; Supabase applies each payment once, whichever arrives first.
    """
    session_id = st.query_params.get("session_id")
    if not session_id:
        return
    del st.query_params["session_id"]  # don't re-process on refresh or share it in the URL
    handled: set[str] = st.session_state.setdefault(_HANDLED_SESSIONS, set())
    if not is_session_id(session_id) or session_id in handled:
        return
    handled.add(session_id)

    stripe_client = get_stripe_client()
    if stripe_client is None or supabase is None:
        _set_notice("info", "Recibimos tu regreso del pago. Tu plan se activará automáticamente en unos minutos.")
        return

    with st.spinner("Confirmando tu pago…"):
        result = fulfill_checkout_session(stripe_client, supabase, session_id, plan_links(), price_plans())
    plan = _plan_title(result.plan)
    if result.status in ("applied", "duplicate"):
        until = "sin vencimiento" if result.exp_date is None else f"hasta el {result.exp_date:%d/%m/%Y}"
        _set_notice("success", f"✅ Pago confirmado. Plan {plan} activo {until}.")
    elif result.status == "pending":
        _set_notice(
            "info",
            f"✅ Pago recibido para {mask_email(result.email)}. Crea tu cuenta con ese mismo correo en "
            f"**Registrarse** y tu plan {plan} se activará automáticamente.",
        )
    elif result.status == "awaiting_payment":
        _set_notice(
            "info",
            "⏳ Tu pago está en proceso (los pagos en OXXO pueden tardar hasta 3 días hábiles). "
            "Tu plan se activará automáticamente al confirmarse.",
        )
    else:
        _set_notice(
            "warning",
            "No pudimos confirmar tu pago en este momento. Si ya pagaste, tu plan se activará "
            "automáticamente en unos minutos; si no, contáctanos.",
        )


# --------------------------------------------------------------------- gate
def require_auth() -> Account:
    """Return the authenticated account, or render the access screen / paywall and stop the script."""
    supabase = get_supabase_store()
    handle_payment_return(supabase)
    static = StaticUserStore(load_user_directory())
    email, token = st.session_state.get(_EMAIL), st.session_state.get(_TOKEN)
    source = st.session_state.get(_SOURCE, static.name)

    if not (email and token):
        _render_access_screen(supabase, static)
        st.stop()

    verdict = get_session_registry().validate(email, token)
    if verdict == "REEMPLAZADA":
        _end_session(("warning", "Tu sesión se cerró porque esta cuenta se abrió en otro dispositivo o navegador."))
        st.rerun()
    if verdict == "EXPIRADA":
        _end_session(("info", "Tu sesión expiró. Vuelve a iniciar sesión."))
        st.rerun()

    # Re-read the account on every interaction so renewals, plan changes and deactivations apply at once.
    try:
        if source == "supabase":
            if supabase is None:
                raise UserStoreError("El servicio de cuentas no está disponible. Intenta más tarde.")
            user = supabase.get_user(email)
        else:
            user = static.get_user(email)
    except UserStoreError as exc:
        _render_retry(f"No pudimos verificar tu suscripción. {exc}")
        st.stop()

    if user is None:
        get_session_registry().release(email, token)
        _end_session(("warning", "Tu cuenta ya no está disponible. Contacta a soporte."))
        st.rerun()

    status = evaluate_membership(user)
    if not status.allowed:
        _render_header()
        _show_notice()
        render_paywall(user, status)
        st.divider()
        if st.button("Cerrar sesión"):
            logout()
            st.rerun()
        st.stop()

    _show_notice()  # e.g. welcome message, or "another session with this account was closed"
    return Account(user=user, status=status, source=source)


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
            renewal_url = _plan_url(user.plan_type, user.email)
            if renewal_url:
                st.link_button("💳 Renovar ahora", renewal_url, width="stretch")

        if st.button("🚪 Cerrar Sesión", width="stretch"):
            logout()
            st.rerun()
        st.divider()
