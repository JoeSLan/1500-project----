// LegalSite Auditor — Supabase Edge Function "stripe-webhook"
//
// Recibe los eventos de Stripe, verifica la firma y actualiza la vigencia en public.users
// llamando a public.apply_stripe_payment / apply_stripe_renewal (sql/stripe_payments.sql).
//
// Eventos:
//   checkout.session.completed            → pago con tarjeta (o pendiente si es OXXO)
//   checkout.session.async_payment_succeeded → OXXO / pagos diferidos confirmados
//   invoice.paid (subscription_cycle)     → renovación automática de suscripciones
//
// Secrets (Supabase → Edge Functions → Secrets):
//   STRIPE_SECRET_KEY      sk_test_... / sk_live_...           (obligatorio)
//   STRIPE_WEBHOOK_SECRET  whsec_... del endpoint en Stripe      (obligatorio)
//   STRIPE_PLAN_LINKS      JSON {"mensual": url, "anual": url, "vitalicio": url}  (opcional; default = links de prueba)
//   STRIPE_PRICE_PLANS     JSON {"price_...": "mensual", ...}   (opcional; alternativa al mapeo por link)
// SUPABASE_URL y SUPABASE_SERVICE_ROLE_KEY los inyecta Supabase automáticamente.
//
// IMPORTANTE: desactiva "Verify JWT" para esta función (Stripe no envía un JWT de Supabase).

import Stripe from "npm:stripe@17.7.0";
import { createClient } from "npm:@supabase/supabase-js@2.118.0";

type Plan = "mensual" | "anual" | "vitalicio";
const PLANS: readonly Plan[] = ["mensual", "anual", "vitalicio"];

const DEFAULT_PLAN_LINKS: Record<Plan, string> = {
  mensual: "https://buy.stripe.com/test_eVqeV6c159ns20n3jSgA800",
  anual: "https://buy.stripe.com/test_14A00c5CHbvA6gDcUsgA801",
  vitalicio: "https://buy.stripe.com/test_28EfZafdhczEeN9f2AgA802",
};
const PAID_STATUSES = new Set(["paid", "no_payment_required"]);

class WebhookError extends Error {
  constructor(message: string, readonly status = 500) {
    super(message);
  }
}

// ------------------------------------------------------------------ config
function parseJson<T>(name: string): T | null {
  const raw = Deno.env.get(name);
  if (!raw) return null;
  try {
    return JSON.parse(raw) as T;
  } catch {
    console.error(`${name} no es JSON válido; se ignora.`);
    return null;
  }
}

function serviceKey(): string | undefined {
  const legacy = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY");
  if (legacy) return legacy;
  const keys = parseJson<Record<string, string>>("SUPABASE_SECRET_KEYS");
  return keys ? Object.values(keys)[0] : undefined;
}

function stripeOptions(): Stripe.StripeConfig {
  const options: Stripe.StripeConfig = { httpClient: Stripe.createFetchHttpClient(), maxNetworkRetries: 2 };
  const base = Deno.env.get("STRIPE_API_BASE"); // solo para pruebas locales
  if (base) {
    const url = new URL(base);
    Object.assign(options, { host: url.hostname, port: url.port, protocol: url.protocol.replace(":", "") });
  }
  return options;
}

const config = {
  stripeKey: Deno.env.get("STRIPE_SECRET_KEY") ?? "",
  webhookSecret: Deno.env.get("STRIPE_WEBHOOK_SECRET") ?? "",
  supabaseUrl: Deno.env.get("SUPABASE_URL") ?? "",
  supabaseKey: serviceKey() ?? "",
  planLinks: { ...DEFAULT_PLAN_LINKS, ...(parseJson<Partial<Record<Plan, string>>>("STRIPE_PLAN_LINKS") ?? {}) },
  pricePlans: parseJson<Record<string, Plan>>("STRIPE_PRICE_PLANS") ?? {},
};

const missing = Object.entries({
  STRIPE_SECRET_KEY: config.stripeKey,
  STRIPE_WEBHOOK_SECRET: config.webhookSecret,
  SUPABASE_URL: config.supabaseUrl,
  SUPABASE_SERVICE_ROLE_KEY: config.supabaseKey,
}).filter(([, v]) => !v).map(([k]) => k);
if (missing.length) console.error(`Faltan secrets: ${missing.join(", ")}`);
if (config.stripeKey && !/^(sk|rk)_(test|live)_/.test(config.stripeKey)) {
  console.error("STRIPE_SECRET_KEY no parece una clave secreta de Stripe (sk_test_/sk_live_).");
}

const stripe = new Stripe(config.stripeKey || "sk_missing", stripeOptions());
const cryptoProvider = Stripe.createSubtleCryptoProvider();
const db = createClient(config.supabaseUrl || "http://localhost", config.supabaseKey || "missing", {
  auth: { persistSession: false, autoRefreshToken: false },
});

// ----------------------------------------------------------------- helpers
const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

function normalizeUrl(value: string): string {
  try {
    const url = new URL(value);
    return `${url.protocol}//${url.host.toLowerCase()}${url.pathname.replace(/\/+$/, "")}`;
  } catch {
    return value.trim();
  }
}

/** client_reference_id = "u_" + base64url(email): lo genera la app (auditor/payments.py). */
function emailFromReference(ref: string | null | undefined): string | null {
  if (!ref || !ref.startsWith("u_")) return null;
  try {
    const b64 = ref.slice(2).replace(/-/g, "+").replace(/_/g, "/");
    const bytes = Uint8Array.from(atob(b64 + "=".repeat((4 - (b64.length % 4)) % 4)), (c) => c.charCodeAt(0));
    const email = new TextDecoder().decode(bytes).trim().toLowerCase();
    return email.includes("@") ? email : null;
  } catch {
    return null;
  }
}

function idOf(value: string | { id: string } | null | undefined): string | null {
  if (!value) return null;
  return typeof value === "string" ? value : value.id;
}

function resolvePlan(session: Stripe.Checkout.Session): Plan | null {
  const fromMetadata = session.metadata?.plan as Plan | undefined;
  if (fromMetadata && PLANS.includes(fromMetadata)) return fromMetadata;

  const link = session.payment_link;
  if (link && typeof link !== "string") {
    const linkPlan = link.metadata?.plan as Plan | undefined;
    if (linkPlan && PLANS.includes(linkPlan)) return linkPlan;
    const target = normalizeUrl(link.url);
    const match = PLANS.find((plan) => normalizeUrl(config.planLinks[plan]) === target);
    if (match) return match;
  }

  for (const item of session.line_items?.data ?? []) {
    const plan = item.price?.id ? config.pricePlans[item.price.id] : undefined;
    if (plan && PLANS.includes(plan)) return plan;
  }
  return null;
}

// ---------------------------------------------------------------- handlers
async function handleCheckout(sessionId: string): Promise<Response> {
  const session = await stripe.checkout.sessions.retrieve(sessionId, { expand: ["line_items", "payment_link"] });

  if (session.status !== "complete" || !PAID_STATUSES.has(session.payment_status)) {
    // OXXO y otros pagos diferidos: llegará checkout.session.async_payment_succeeded.
    console.log(`Sesión ${session.id} aún sin pagar (${session.payment_status}); se espera confirmación.`);
    return json(200, { status: "awaiting_payment" });
  }

  const email = emailFromReference(session.client_reference_id) ??
    (session.customer_details?.email ?? session.customer_email ?? "").trim().toLowerCase();
  if (!email) throw new WebhookError(`Sesión ${session.id} sin correo identificable.`);

  const plan = resolvePlan(session);
  if (!plan) {
    const link = typeof session.payment_link === "string" ? session.payment_link : session.payment_link?.url;
    throw new WebhookError(
      `No se pudo identificar el plan de la sesión ${session.id} (payment_link: ${link}). ` +
        "Revisa STRIPE_PLAN_LINKS o STRIPE_PRICE_PLANS.",
    );
  }

  const { data, error } = await db.rpc("apply_stripe_payment", {
    p_ref: session.id,
    p_email: email,
    p_plan: plan,
    p_subscription_id: idOf(session.subscription as string | { id: string } | null),
    p_customer_id: idOf(session.customer as string | { id: string } | null),
    p_amount: session.amount_total,
    p_currency: session.currency,
    p_source: "webhook",
  });
  if (error) throw new WebhookError(`Supabase: ${error.message}`);
  console.log(`Pago ${session.id} → ${email} (${plan}): ${JSON.stringify(data)}`);
  return json(200, data);
}

async function handleInvoicePaid(invoice: Stripe.Invoice): Promise<Response> {
  if (invoice.billing_reason !== "subscription_cycle") {
    // El primer cobro de la suscripción ya llega como checkout.session.completed.
    return json(200, { status: "ignored", billing_reason: invoice.billing_reason });
  }
  // deno-lint-ignore no-explicit-any
  const raw = invoice as any;
  const subscriptionId = idOf(raw.subscription ?? raw.parent?.subscription_details?.subscription);
  if (!subscriptionId) throw new WebhookError(`Factura ${invoice.id} sin suscripción.`);

  const { data, error } = await db.rpc("apply_stripe_renewal", {
    p_ref: invoice.id,
    p_subscription_id: subscriptionId,
    p_amount: invoice.amount_paid,
    p_currency: invoice.currency,
  });
  if (error) throw new WebhookError(`Supabase: ${error.message}`);
  if (data?.status === "unknown_subscription") {
    // Puede llegar antes que el checkout original: 500 hace que Stripe reintente.
    throw new WebhookError(`Suscripción ${subscriptionId} desconocida todavía.`);
  }
  console.log(`Renovación ${invoice.id} (${subscriptionId}): ${JSON.stringify(data)}`);
  return json(200, data);
}

// ------------------------------------------------------------------ server
Deno.serve(async (req) => {
  if (req.method !== "POST") return json(405, { error: "Método no permitido" });
  if (missing.length) return json(500, { error: `Faltan secrets: ${missing.join(", ")}` });

  const signature = req.headers.get("Stripe-Signature");
  if (!signature) return json(400, { error: "Falta Stripe-Signature" });

  const body = await req.text();
  let event: Stripe.Event;
  try {
    event = await stripe.webhooks.constructEventAsync(body, signature, config.webhookSecret, undefined, cryptoProvider);
  } catch (err) {
    console.error("Firma inválida:", (err as Error).message);
    return json(400, { error: "Firma inválida" });
  }

  try {
    switch (event.type) {
      case "checkout.session.completed":
      case "checkout.session.async_payment_succeeded":
        return await handleCheckout((event.data.object as Stripe.Checkout.Session).id);
      case "invoice.paid":
        return await handleInvoicePaid(event.data.object as Stripe.Invoice);
      default:
        return json(200, { status: "ignored", type: event.type });
    }
  } catch (err) {
    // 5xx → Stripe reintenta durante ~3 días; el error queda visible en Stripe → Webhooks.
    console.error(`Error procesando ${event.type} (${event.id}):`, (err as Error).message);
    return json(err instanceof WebhookError ? err.status : 500, { error: (err as Error).message });
  }
});
