"""Stripe integration on the app side (no Streamlit imports).

- Builds the Payment Link URL for each plan with the user's email (prefilled_email) and a
  client_reference_id the webhook can decode back to the account email.
- Validates st.secrets["stripe"]["secret_key"].
- Fulfills a Checkout Session when the buyer returns to the app (?session_id=cs_...). The
  webhook (supabase/functions/stripe-webhook) does the same; Supabase guarantees each payment
  is applied only once, whichever arrives first.
"""

from __future__ import annotations

import base64
import binascii
import logging
import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from auditor.auth import PLAN_TYPES, normalize_email

logger = logging.getLogger(__name__)

DEFAULT_PLAN_LINKS: dict[str, str] = {
    "mensual": "https://buy.stripe.com/test_eVqeV6c159ns20n3jSgA800",
    "anual": "https://buy.stripe.com/test_14A00c5CHbvA6gDcUsgA801",
    "vitalicio": "https://buy.stripe.com/test_28EfZafdhczEeN9f2AgA802",
}
PAID_STATUSES = {"paid", "no_payment_required"}
CLIENT_REFERENCE_MAX = 200  # Stripe limit; longer values are silently dropped
_SECRET_KEY_RE = re.compile(r"^(sk|rk)_(test|live)_[A-Za-z0-9]{10,}$")
_SESSION_ID_RE = re.compile(r"^cs_(test|live)_[A-Za-z0-9]{10,200}$")


class PaymentConfigError(ValueError):
    """Stripe configuration problem (message safe to log, not shown to end users)."""


# ------------------------------------------------------------ configuration
@dataclass(frozen=True)
class StripeConfig:
    secret_key: str

    @property
    def mode(self) -> str:
        return "test" if "_test_" in self.secret_key else "live"


def validate_secret_key(raw: Any) -> StripeConfig:
    """Validate st.secrets["stripe"]["secret_key"]."""
    key = str(raw or "").strip()
    if not key:
        raise PaymentConfigError("Falta st.secrets['stripe']['secret_key'].")
    if key.startswith("pk_"):
        raise PaymentConfigError("stripe.secret_key es la clave PUBLICABLE (pk_...); usa la secreta (sk_...).")
    if key.startswith("whsec_"):
        raise PaymentConfigError("stripe.secret_key es el secreto del webhook (whsec_...); usa la clave sk_...")
    if not _SECRET_KEY_RE.match(key):
        raise PaymentConfigError("stripe.secret_key no tiene el formato sk_test_... / sk_live_...")
    return StripeConfig(secret_key=key)


def links_mode(plan_links: Mapping[str, str]) -> str | None:
    """'test', 'live' or None (non-Stripe or mixed links)."""
    modes = {("test" if "/test_" in url else "live") for url in plan_links.values() if "stripe.com" in url}
    return modes.pop() if len(modes) == 1 else None


def merge_plan_links(overrides: Mapping[str, Any] | None) -> dict[str, str]:
    """DEFAULT_PLAN_LINKS overridden by [saas.plans.<plan>].url from secrets."""
    links = dict(DEFAULT_PLAN_LINKS)
    for plan in PLAN_TYPES:
        url = ((overrides or {}).get(plan) or {}).get("url")
        if url:
            links[plan] = str(url)
    return links


# --------------------------------------------------- client_reference_id
def client_reference_for(email: str) -> str | None:
    """'u_' + base64url(email) without padding: only [A-Za-z0-9_-], as Stripe requires."""
    encoded = base64.urlsafe_b64encode(normalize_email(email).encode("utf-8")).decode("ascii").rstrip("=")
    reference = f"u_{encoded}"
    return reference if len(reference) <= CLIENT_REFERENCE_MAX else None


def email_from_reference(reference: str | None) -> str | None:
    if not reference or not reference.startswith("u_"):
        return None
    payload = reference[2:]
    try:
        email = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    email = normalize_email(email)
    return email if "@" in email else None


def checkout_url(base_url: str, email: str | None) -> str:
    """Payment link for a user: fills {email} templates and, for Stripe links, adds
    prefilled_email + client_reference_id so the payment lands on the right account."""
    url = base_url.replace("{email}", quote(email or "", safe=""))
    if not email or "stripe.com" not in (urlsplit(url).hostname or ""):
        return url
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    if not query.get("prefilled_email"):
        query["prefilled_email"] = normalize_email(email)
    reference = client_reference_for(email)
    if reference:
        query["client_reference_id"] = reference
    return urlunsplit(parts._replace(query=urlencode(query)))


# ---------------------------------------------------------------- sessions
def is_session_id(value: Any) -> bool:
    return isinstance(value, str) and bool(_SESSION_ID_RE.match(value))


def _normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    return f"{parts.scheme}://{(parts.hostname or '').lower()}{parts.path.rstrip('/')}"


def _id(value: Any) -> str | None:
    if isinstance(value, Mapping):
        return value.get("id")
    return value or None


def session_email(session: Mapping[str, Any]) -> str | None:
    """Account email: client_reference_id (set by the app) first, then the email typed at checkout."""
    email = email_from_reference(session.get("client_reference_id"))
    if email:
        return email
    details = session.get("customer_details") or {}
    raw = details.get("email") or session.get("customer_email")
    return normalize_email(raw) if raw else None


def resolve_plan(
    session: Mapping[str, Any], plan_links: Mapping[str, str], price_plans: Mapping[str, str] | None = None
) -> str | None:
    """Plan bought in a Checkout Session expanded with ['line_items', 'payment_link']."""
    plan = (session.get("metadata") or {}).get("plan")
    if plan in PLAN_TYPES:
        return plan
    link = session.get("payment_link")
    if isinstance(link, Mapping):
        plan = (link.get("metadata") or {}).get("plan")
        if plan in PLAN_TYPES:
            return plan
        target = _normalize_url(str(link.get("url") or ""))
        for candidate in PLAN_TYPES:
            if candidate in plan_links and _normalize_url(plan_links[candidate]) == target:
                return candidate
    for item in (session.get("line_items") or {}).get("data") or []:
        plan = (price_plans or {}).get(_id(item.get("price")) or "")
        if plan in PLAN_TYPES:
            return plan
    return None


@dataclass(frozen=True)
class FulfillmentResult:
    status: str  # applied | duplicate | pending | awaiting_payment | error
    email: str | None = None
    plan: str | None = None
    exp_date: date | None = None
    detail: str | None = None


def fulfill_checkout_session(
    stripe_client: Any,
    store: Any,
    session_id: str,
    plan_links: Mapping[str, str],
    price_plans: Mapping[str, str] | None = None,
) -> FulfillmentResult:
    """Retrieve a Checkout Session from Stripe and apply it in Supabase (idempotent)."""
    if not is_session_id(session_id):
        return FulfillmentResult("error", detail="session_id inválido")
    try:
        session_obj = stripe_client.v1.checkout.sessions.retrieve(
            session_id, params={"expand": ["line_items", "payment_link"]}
        )
    except Exception as exc:  # stripe.StripeError and network errors
        logger.error("Stripe: no se pudo leer la sesión %s: %s", session_id, exc)
        return FulfillmentResult("error", detail=f"Stripe: {type(exc).__name__}")
    session = session_obj.to_dict() if hasattr(session_obj, "to_dict") else dict(session_obj)

    if session.get("status") != "complete" or session.get("payment_status") not in PAID_STATUSES:
        return FulfillmentResult("awaiting_payment", email=session_email(session))

    email = session_email(session)
    plan = resolve_plan(session, plan_links, price_plans)
    if not email or not plan:
        logger.error("Stripe: sesión %s sin correo (%s) o plan (%s) identificable.", session_id, email, plan)
        return FulfillmentResult("error", email=email, plan=plan, detail="plan o correo no identificable")

    try:
        result = store.apply_payment(
            ref=session["id"],
            email=email,
            plan=plan,
            subscription_id=_id(session.get("subscription")),
            customer_id=_id(session.get("customer")),
            amount=session.get("amount_total"),
            currency=session.get("currency"),
        )
    except Exception as exc:
        logger.error("Supabase: no se pudo aplicar el pago %s: %s", session_id, exc)
        return FulfillmentResult("error", email=email, plan=plan, detail="base de datos")

    exp = result.get("exp_date")
    return FulfillmentResult(
        status=str(result.get("status", "error")),
        email=email,
        plan=result.get("plan") or plan,
        exp_date=date.fromisoformat(exp) if exp else None,
    )


def mask_email(email: str | None) -> str:
    if not email or "@" not in email:
        return "tu correo"
    user, domain = email.split("@", 1)
    return f"{user[:1]}{'*' * max(len(user) - 1, 2)}@{domain}"
