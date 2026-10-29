import re
from datetime import date
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from auditor.payments import (
    DEFAULT_PLAN_LINKS,
    PaymentConfigError,
    checkout_url,
    client_reference_for,
    email_from_reference,
    fulfill_checkout_session,
    is_session_id,
    links_mode,
    mask_email,
    merge_plan_links,
    resolve_plan,
    session_email,
    validate_secret_key,
)

SESSION_ID = "cs_test_a1B2c3D4e5F6g7H8i9J0"


# ------------------------------------------------------------------ config
@pytest.mark.parametrize(
    ("key", "error"),
    [
        ("sk_test_51ABCdefGHIjklMNO", None),
        ("sk_live_51ABCdefGHIjklMNO", None),
        ("rk_test_51ABCdefGHIjklMNO", None),
        ("", "Falta"),
        ("pk_test_51ABCdefGHIjklMNO", "PUBLICABLE"),
        ("whsec_abc123abc123abc", "webhook"),
        ("sk_test_corta", "formato"),
        ("hola", "formato"),
    ],
)
def test_validate_secret_key(key, error):
    if error is None:
        config = validate_secret_key(key)
        assert config.mode == ("test" if "_test_" in key else "live")
    else:
        with pytest.raises(PaymentConfigError, match=error):
            validate_secret_key(key)


def test_default_links_are_the_test_payment_links():
    assert DEFAULT_PLAN_LINKS == {
        "mensual": "https://buy.stripe.com/test_eVqeV6c159ns20n3jSgA800",
        "anual": "https://buy.stripe.com/test_14A00c5CHbvA6gDcUsgA801",
        "vitalicio": "https://buy.stripe.com/test_28EfZafdhczEeN9f2AgA802",
    }
    assert links_mode(DEFAULT_PLAN_LINKS) == "test"
    assert links_mode({"mensual": "https://buy.stripe.com/abc", "anual": "https://buy.stripe.com/def"}) == "live"
    assert links_mode({"mensual": "https://buy.stripe.com/test_a", "anual": "https://buy.stripe.com/b"}) is None


def test_merge_plan_links_overrides_from_secrets():
    links = merge_plan_links({"anual": {"url": "https://buy.stripe.com/live_anual", "price": "$1"}})
    assert links["anual"] == "https://buy.stripe.com/live_anual"
    assert links["mensual"] == DEFAULT_PLAN_LINKS["mensual"]


# ------------------------------------------------------ client reference
@pytest.mark.parametrize("email", ["cliente@correo.mx", "Nombre.Apellido+tag@sub.dominio.com.mx", "ñandú@ejemplo.mx"])
def test_client_reference_roundtrip_and_allowed_chars(email):
    reference = client_reference_for(email)
    assert reference is not None and re.fullmatch(r"[A-Za-z0-9_-]{1,200}", reference)
    assert email_from_reference(reference) == email.strip().lower()


def test_client_reference_limits_and_garbage():
    assert client_reference_for("a" * 160 + "@x.mx") is None  # would exceed Stripe's 200 chars
    assert email_from_reference(None) is None
    assert email_from_reference("cart_123") is None
    assert email_from_reference("u_!!!") is None


def test_checkout_url_personalizes_stripe_links():
    url = checkout_url(DEFAULT_PLAN_LINKS["anual"], "Cliente@Correo.MX")
    parts = urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == DEFAULT_PLAN_LINKS["anual"]
    query = parse_qs(parts.query)
    assert query["prefilled_email"] == ["cliente@correo.mx"]
    assert email_from_reference(query["client_reference_id"][0]) == "cliente@correo.mx"


def test_checkout_url_keeps_existing_params_and_templates():
    url = checkout_url("https://buy.stripe.com/test_x?locale=es&prefilled_email={email}", "a@b.mx")
    query = parse_qs(urlsplit(url).query)
    assert query["locale"] == ["es"] and query["prefilled_email"] == ["a@b.mx"] and "client_reference_id" in query
    assert checkout_url("https://pago.example/m?e={email}", "a@b.mx") == "https://pago.example/m?e=a%40b.mx"
    assert checkout_url(DEFAULT_PLAN_LINKS["mensual"], None) == DEFAULT_PLAN_LINKS["mensual"]


# ---------------------------------------------------------- plan resolution
def _session(**overrides):
    session = {
        "id": SESSION_ID,
        "object": "checkout.session",
        "status": "complete",
        "payment_status": "paid",
        "client_reference_id": client_reference_for("cliente@correo.mx"),
        "customer_details": {"email": "otro@correo.mx"},
        "customer_email": None,
        "payment_link": {"id": "plink_1", "url": DEFAULT_PLAN_LINKS["anual"] + "/", "metadata": {}},
        "line_items": {"data": [{"price": {"id": "price_ANUAL"}}]},
        "metadata": {},
        "subscription": "sub_123",
        "customer": "cus_123",
        "amount_total": 499000,
        "currency": "mxn",
    }
    session.update(overrides)
    return session


def test_resolve_plan_by_payment_link_url():
    assert resolve_plan(_session(), DEFAULT_PLAN_LINKS) == "anual"


def test_resolve_plan_precedence_and_fallbacks():
    assert resolve_plan(_session(metadata={"plan": "vitalicio"}), DEFAULT_PLAN_LINKS) == "vitalicio"
    link_meta = {"id": "plink_2", "url": "https://buy.stripe.com/otra", "metadata": {"plan": "mensual"}}
    assert resolve_plan(_session(payment_link=link_meta), DEFAULT_PLAN_LINKS) == "mensual"
    unknown_link = _session(payment_link={"id": "plink_3", "url": "https://buy.stripe.com/otra", "metadata": {}})
    assert resolve_plan(unknown_link, DEFAULT_PLAN_LINKS) is None  # never guess a plan
    assert resolve_plan(unknown_link, DEFAULT_PLAN_LINKS, {"price_ANUAL": "anual"}) == "anual"
    assert resolve_plan(_session(payment_link="plink_1"), DEFAULT_PLAN_LINKS) is None  # not expanded


def test_session_email_prefers_reference():
    assert session_email(_session()) == "cliente@correo.mx"
    assert session_email(_session(client_reference_id=None)) == "otro@correo.mx"
    assert session_email(_session(client_reference_id=None, customer_details=None, customer_email="C@D.mx")) == "c@d.mx"


# ------------------------------------------------------------- fulfillment
class FakeStripe:
    def __init__(self, session=None, error=None):
        self.calls = []

        def retrieve(session_id, params=None):
            self.calls.append((session_id, params))
            if error:
                raise error
            return SimpleNamespace(to_dict=lambda: session)

        self.v1 = SimpleNamespace(checkout=SimpleNamespace(sessions=SimpleNamespace(retrieve=retrieve)))


class FakeStore:
    def __init__(self, response=None, error=None):
        self.calls, self.response, self.error = [], response, error

    def apply_payment(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.response


def test_fulfill_paid_session():
    stripe_client = FakeStripe(_session())
    store = FakeStore({"status": "applied", "plan": "anual", "exp_date": "2027-10-29", "email": "cliente@correo.mx"})
    result = fulfill_checkout_session(stripe_client, store, SESSION_ID, DEFAULT_PLAN_LINKS)
    assert (result.status, result.plan, result.exp_date) == ("applied", "anual", date(2027, 10, 29))
    assert stripe_client.calls == [(SESSION_ID, {"expand": ["line_items", "payment_link"]})]
    assert store.calls == [{
        "ref": SESSION_ID, "email": "cliente@correo.mx", "plan": "anual", "subscription_id": "sub_123",
        "customer_id": "cus_123", "amount": 499000, "currency": "mxn",
    }]


def test_fulfill_lifetime_has_no_expiration():
    store = FakeStore({"status": "applied", "plan": "vitalicio", "exp_date": None})
    result = fulfill_checkout_session(FakeStripe(_session(metadata={"plan": "vitalicio"})), store, SESSION_ID, DEFAULT_PLAN_LINKS)
    assert result.status == "applied" and result.exp_date is None


@pytest.mark.parametrize(
    ("session", "expected"),
    [
        (_session(payment_status="unpaid"), "awaiting_payment"),  # OXXO voucher not paid yet
        (_session(status="open"), "awaiting_payment"),
        (_session(payment_link={"id": "p", "url": "https://buy.stripe.com/x", "metadata": {}}, line_items=None), "error"),
        (_session(client_reference_id=None, customer_details=None), "error"),
    ],
)
def test_fulfill_does_not_apply(session, expected):
    store = FakeStore({"status": "applied"})
    assert fulfill_checkout_session(FakeStripe(session), store, SESSION_ID, DEFAULT_PLAN_LINKS).status == expected
    assert store.calls == []


def test_fulfill_rejects_bad_ids_and_backend_errors():
    store = FakeStore({"status": "applied"})
    fake = FakeStripe(_session())
    assert fulfill_checkout_session(fake, store, "cs_test_<script>", DEFAULT_PLAN_LINKS).status == "error"
    assert fake.calls == []  # never sent to Stripe
    assert fulfill_checkout_session(FakeStripe(error=RuntimeError("red")), store, SESSION_ID, DEFAULT_PLAN_LINKS).status == "error"
    failing = FakeStore(error=RuntimeError("db"))
    assert fulfill_checkout_session(FakeStripe(_session()), failing, SESSION_ID, DEFAULT_PLAN_LINKS).status == "error"


def test_helpers():
    assert is_session_id(SESSION_ID) and not is_session_id("pi_123") and not is_session_id(None)
    assert mask_email("cliente@correo.mx") == "c******@correo.mx"
    assert mask_email(None) == "tu correo"
