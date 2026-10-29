"""End-to-end access flows with Streamlit's AppTest and an in-memory Supabase (no network, no browser)."""

import json
from datetime import timedelta
from pathlib import Path

from types import SimpleNamespace

import httpx
import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from auditor import auth_ui, user_store
from auditor.auth import hash_password, today_mx
from auditor.payments import DEFAULT_PLAN_LINKS, client_reference_for, email_from_reference
from tests.fake_supabase import FakeSupabase

APP = str(Path(__file__).resolve().parent.parent / "app.py")
PASSWORD = "Secreta123"
PLAN_URLS = {
    "mensual": {"price": "$499 MXN", "url": "https://pago.example/mensual?prefilled_email={email}"},
    "anual": {"price": "$4,990 MXN", "url": "https://pago.example/anual"},
    "vitalicio": {"price": "$12,990 MXN", "url": "https://pago.example/lifetime"},
}


def _row(email, plan="mensual", days=30, active=True):
    exp = "" if plan == "vitalicio" else (today_mx() + timedelta(days=days)).isoformat()
    return {
        "email": email,
        "password_hash": hash_password(PASSWORD, iterations=1_000),
        "plan_type": plan,
        "exp_date": exp,
        "is_active": active,
        "is_lifetime": plan == "vitalicio",
    }


@pytest.fixture
def db(tmp_path, monkeypatch):
    fake = FakeSupabase()
    fake.tables["users"] = [_row("cliente@x.com", days=30), _row("vencido@x.com", days=-1), _row("baja@x.com", active=False)]
    monkeypatch.setattr(
        user_store.SupabaseUserStore, "from_credentials", classmethod(lambda cls, url, key, table: cls(fake, table))
    )
    static = tmp_path / "users.json"
    static.write_text(json.dumps({"users": [_row("admin@x.com", plan="vitalicio")]}), encoding="utf-8")
    monkeypatch.setenv("LEGALSITE_USERS_FILE", str(static))
    st.cache_resource.clear()  # fresh session registry, limiter and store per test
    return fake


def _app(supabase=True, saas=None) -> AppTest:
    at = AppTest.from_file(APP, default_timeout=60)
    if supabase:
        at.secrets["supabase"] = {"url": "https://demo.supabase.co", "key": "sb_secret_test"}
    at.secrets["saas"] = saas if saas is not None else {"plans": PLAN_URLS, "support_email": "soporte@x.mx"}
    return at.run()


def _text(at: AppTest) -> str:
    kinds = ("markdown", "caption", "error", "warning", "info", "success", "title", "subheader", "header")
    return "\n".join(str(e.value) for kind in kinds for e in at.get(kind))


def _has_tool(at: AppTest) -> bool:
    return any(r.label == "Origen del documento" for r in at.radio)


def _click(at: AppTest, label: str) -> AppTest:
    next(b for b in at.button if b.label == label).click()
    return at.run()


def _login(at: AppTest, email: str, password: str = PASSWORD) -> AppTest:
    at.text_input(key="login_email").input(email)
    at.text_input(key="login_password").input(password)
    return _click(at, "Entrar")


def _signup(at: AppTest, email: str, password: str = PASSWORD, confirm: str | None = None) -> AppTest:
    at.text_input(key="signup_email").input(email)
    at.text_input(key="signup_password").input(password)
    at.text_input(key="signup_confirm").input(password if confirm is None else confirm)
    return _click(at, "Crear cuenta")


def _plan_links(at: AppTest) -> list[str]:
    return [b.proto.url for b in at.get("link_button")]


# ---------------------------------------------------------------- landing
def test_access_screen_has_tabs_and_three_plans(db):
    at = _app()
    assert not at.exception
    assert [t.label for t in at.tabs] == ["Iniciar sesión", "Registrarse"]
    text = _text(at)
    assert "Planes de suscripción" in text
    assert all(t in text for t in ("Mensual", "Anual", "De por vida (Lifetime)", "$499 MXN", "$12,990 MXN"))
    assert _plan_links(at) == [
        "https://pago.example/mensual?prefilled_email=",
        "https://pago.example/anual",
        "https://pago.example/lifetime",
    ]
    assert not _has_tool(at)


# ---------------------------------------------------------------- sign-up
def test_signup_gives_three_day_trial_and_logs_in(db):
    at = _signup(_app(), "Nueva@Correo.MX")
    assert not at.exception
    row = next(r for r in db.rows() if r["email"] == "nueva@correo.mx")
    assert row["plan_type"] == "mensual" and row["is_active"] is True
    assert row["exp_date"] == (today_mx() + timedelta(days=3)).isoformat()
    assert PASSWORD not in json.dumps(row)
    assert _has_tool(at)
    text = _text(at)
    assert "prueba gratis" in text and "3 días restantes" in text


def test_signup_trial_length_configurable(db):
    _signup(_app(saas={"plans": PLAN_URLS, "signup_days": 7}), "siete@x.com")
    row = next(r for r in db.rows() if r["email"] == "siete@x.com")
    assert row["exp_date"] == (today_mx() + timedelta(days=7)).isoformat()


@pytest.mark.parametrize(
    ("email", "password", "confirm", "expected"),
    [("cliente@x.com", PASSWORD, None, "Ya existe una cuenta"), ("admin@x.com", PASSWORD, None, "Ya existe una cuenta"),
     ("nuevo@x.com", PASSWORD, "otra-clave", "no coinciden"), ("nuevo@x.com", "corta", None, "8 caracteres")],
)
def test_signup_rejections(db, email, password, confirm, expected):
    before = len(db.rows())
    at = _signup(_app(), email, password, confirm)
    assert expected in _text(at)
    assert len(db.rows()) == before and not _has_tool(at)


def test_signup_requiring_payment_lands_on_plans(db):
    at = _signup(_app(saas={"plans": PLAN_URLS, "signup_days": -1}), "paga@x.com")
    assert not _has_tool(at)
    assert "venció" in _text(at) and "Elige tu plan" in _text(at)
    assert "https://pago.example/mensual?prefilled_email=paga%40x.com" in _plan_links(at)


def test_signup_disabled_without_supabase(db):
    at = _app(supabase=False)
    assert "registro en línea no está disponible" in _text(at)
    assert not any(b.label == "Crear cuenta" for b in at.button)


def test_signup_rate_limited_per_session(db):
    at = _app()
    for i in range(3):
        at = _signup(at, f"bot{i}@x.com")
        at = _click(at, "🚪 Cerrar Sesión")
    at = _signup(at, "bot9@x.com")
    assert "límite de registros" in _text(at)
    assert not any(r["email"] == "bot9@x.com" for r in db.rows())


# ------------------------------------------------------------------ login
def test_login_with_supabase_account(db):
    at = _login(_app(), "CLIENTE@x.com")
    assert _has_tool(at) and "cliente@x.com" in _text(at)


def test_wrong_password(db):
    at = _login(_app(), "cliente@x.com", "incorrecta")
    assert "Correo o contraseña incorrectos" in _text(at) and not _has_tool(at)


def test_secrets_account_fallback(db):
    at = _login(_app(), "admin@x.com")
    assert _has_tool(at) and "Acceso Vitalicio" in _text(at)


def test_expired_account_sees_plans_with_current_plan(db):
    at = _login(_app(), "vencido@x.com")
    assert not _has_tool(at)
    text = _text(at)
    assert "venció" in text and "Tu plan actual" in text
    assert "https://pago.example/mensual?prefilled_email=vencido%40x.com" in _plan_links(at)
    assert [b.label for b in at.get("link_button")] == ["💳 Renovar", "💳 Cambiar a este plan", "💳 Cambiar a este plan"]


def test_deactivated_account_blocked(db):
    at = _login(_app(), "baja@x.com")
    assert "no está activa" in _text(at) and not _has_tool(at)


def test_renewal_in_database_unlocks_live_session(db):
    at = _login(_app(), "vencido@x.com")
    assert not _has_tool(at)
    next(r for r in db.rows() if r["email"] == "vencido@x.com")["exp_date"] = (today_mx() + timedelta(days=30)).isoformat()
    at.run()
    assert _has_tool(at)


def test_supabase_outage(db):
    db.fail_with = httpx.ConnectError("caído")
    at = _login(_app(), "cliente@x.com")
    assert "servicio de cuentas" in _text(at) and not _has_tool(at)
    at = _login(_app(), "admin@x.com")  # static accounts keep working
    assert _has_tool(at)


def test_outage_during_session_keeps_user_logged_in(db):
    at = _login(_app(), "cliente@x.com")
    assert _has_tool(at)
    db.fail_with = httpx.ConnectError("caído")
    at.run()
    assert not _has_tool(at) and "No pudimos verificar tu suscripción" in _text(at)
    db.fail_with = None
    at = _click(at, "🔄 Reintentar")
    assert _has_tool(at)


# --------------------------------------------------------------- sessions
def test_single_session_last_login_wins(db):
    first = _login(_app(), "cliente@x.com")
    second = _login(_app(), "cliente@x.com")
    assert _has_tool(second) and "se cerró automáticamente" in _text(second)
    first.run()
    assert not _has_tool(first) and "otro dispositivo" in _text(first)


def test_logout(db):
    at = _click(_login(_app(), "cliente@x.com"), "🚪 Cerrar Sesión")
    assert not _has_tool(at) and "Cerraste sesión" in _text(at)


def test_brute_force_lockout(db):
    at = _app()
    for _ in range(5):
        at = _login(at, "cliente@x.com", "mala")
    at = _login(at, "cliente@x.com")
    assert "Demasiados intentos" in _text(at) and not _has_tool(at)


# ---------------------------------------------------------------- Stripe
SESSION_ID = "cs_test_a1B2c3D4e5F6g7H8i9J0"


class _FakeStripe:
    def __init__(self, session):
        self.session = session
        self.calls = []
        retrieve = lambda sid, params=None: (self.calls.append(sid), SimpleNamespace(to_dict=lambda: self.session))[1]  # noqa: E731
        self.v1 = SimpleNamespace(checkout=SimpleNamespace(sessions=SimpleNamespace(retrieve=retrieve)))


def _paid_session(email="vencido@x.com", plan_url=DEFAULT_PLAN_LINKS["anual"], payment_status="paid"):
    return {
        "id": SESSION_ID, "status": "complete", "payment_status": payment_status,
        "client_reference_id": client_reference_for(email), "customer_details": {"email": email},
        "payment_link": {"id": "plink_1", "url": plan_url, "metadata": {}}, "line_items": {"data": []},
        "metadata": {}, "subscription": None, "customer": "cus_1", "amount_total": 499000, "currency": "mxn",
    }


def _apply_like_sql(db):
    """Minimal stand-in for public.apply_stripe_payment: extends the user one year (anual)."""
    applied: set[str] = set()

    def handler(name, params):
        assert name == "apply_stripe_payment"
        row = next((r for r in db.rows() if r["email"] == params["p_email"]), None)
        if row is None:
            return {"status": "pending", "email": params["p_email"], "plan": params["p_plan"]}
        if params["p_ref"] in applied:
            return {"status": "duplicate", "email": row["email"], "plan": row["plan_type"], "exp_date": row["exp_date"]}
        applied.add(params["p_ref"])
        row["plan_type"], row["exp_date"] = params["p_plan"], (today_mx() + timedelta(days=365)).isoformat()
        return {"status": "applied", "email": row["email"], "plan": row["plan_type"], "exp_date": row["exp_date"]}

    return handler


def _app_with_stripe(db, monkeypatch, session, query=SESSION_ID, saas=None):
    fake_stripe = _FakeStripe(session)
    monkeypatch.setattr(auth_ui, "get_stripe_client", lambda: fake_stripe)
    db.rpc_handler = _apply_like_sql(db)
    at = AppTest.from_file(APP, default_timeout=60)
    at.secrets["supabase"] = {"url": "https://demo.supabase.co", "key": "sb_secret_test"}
    at.secrets["stripe"] = {"secret_key": "sk_test_51ABCdefGHIjklMNO"}
    at.secrets["saas"] = saas or {}
    if query:
        at.query_params["session_id"] = query
    return at.run(), fake_stripe


def test_default_plan_buttons_use_stripe_test_links_with_reference(db):
    at = _login(_app(saas={}), "vencido@x.com")
    links = _plan_links(at)
    assert [u.split("?")[0] for u in links] == list(DEFAULT_PLAN_LINKS.values())
    query = dict(p.split("=", 1) for p in links[1].split("?", 1)[1].split("&"))
    assert query["prefilled_email"] == "vencido%40x.com"
    assert email_from_reference(query["client_reference_id"]) == "vencido@x.com"
    assert "modo de prueba" in _text(at)


def test_return_from_stripe_activates_plan(db, monkeypatch):
    at, fake_stripe = _app_with_stripe(db, monkeypatch, _paid_session())
    assert not at.exception
    assert fake_stripe.calls == [SESSION_ID]
    assert "Pago confirmado. Plan Anual activo hasta el" in _text(at)
    assert "session_id" not in at.query_params
    row = next(r for r in db.rows() if r["email"] == "vencido@x.com")
    assert row["plan_type"] == "anual" and row["exp_date"] > today_mx().isoformat()
    at = _login(at, "vencido@x.com")  # the expired account now has access
    assert _has_tool(at) and "Plan **Anual**" in _text(at)


def test_return_is_idempotent_on_rerun(db, monkeypatch):
    at, fake_stripe = _app_with_stripe(db, monkeypatch, _paid_session())
    at.run()
    assert fake_stripe.calls == [SESSION_ID]  # query param removed: not processed twice


def test_return_before_registering_is_pending(db, monkeypatch):
    at, _ = _app_with_stripe(db, monkeypatch, _paid_session(email="nuevo@x.com"))
    assert "Pago recibido para n****@x.com" in _text(at) and "Registrarse" in _text(at)


def test_return_with_oxxo_pending(db, monkeypatch):
    at, _ = _app_with_stripe(db, monkeypatch, _paid_session(payment_status="unpaid"))
    assert "OXXO" in _text(at)
    assert next(r for r in db.rows() if r["email"] == "vencido@x.com")["exp_date"] < today_mx().isoformat()


@pytest.mark.parametrize("query", ["cs_test_<script>alert(1)</script>", "pi_123"])
def test_return_ignores_invalid_session_ids(db, monkeypatch, query):
    at, fake_stripe = _app_with_stripe(db, monkeypatch, _paid_session(), query=query)
    assert fake_stripe.calls == [] and not at.exception


def test_return_without_stripe_configured(db):
    at = AppTest.from_file(APP, default_timeout=60)
    at.secrets["supabase"] = {"url": "https://demo.supabase.co", "key": "sb_secret_test"}
    at.query_params["session_id"] = SESSION_ID
    at.run()
    assert "se activará automáticamente en unos minutos" in _text(at)
