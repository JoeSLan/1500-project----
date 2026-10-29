-- LegalSite Auditor — pagos de Stripe → vigencia automática en public.users
-- Ejecuta este script completo en Supabase → SQL Editor → Run, DESPUÉS de sql/supabase_schema.sql.
-- Es idempotente: puedes volver a ejecutarlo sin perder datos.
--
-- Lo usan tanto el webhook (Edge Function stripe-webhook) como la app al volver del pago.
-- Toda la lógica vive aquí para que un pago se aplique UNA sola vez aunque llegue por los dos caminos.

-- ---------------------------------------------------------------- tabla de pagos
create table if not exists public.payments (
  id                     bigint generated always as identity primary key,
  stripe_ref             text        not null unique,      -- cs_... (Checkout Session) o in_... (factura de renovación)
  email                  text        not null,
  plan_type              text        not null check (plan_type in ('mensual', 'anual', 'vitalicio')),
  stripe_subscription_id text,
  stripe_customer_id     text,
  amount                 bigint,                            -- en centavos, como lo reporta Stripe
  currency               text,
  source                 text        not null default 'webhook',  -- webhook | app
  status                 text        not null default 'pending' check (status in ('pending', 'applied')),
  exp_date_after         date,                              -- vigencia resultante (null = de por vida)
  created_at             timestamptz not null default now(),
  applied_at             timestamptz
);

create index if not exists payments_email_idx        on public.payments (email);
create index if not exists payments_subscription_idx on public.payments (stripe_subscription_id);

-- RLS sin políticas: solo la service_role key (servidor) puede leer o escribir.
alter table public.payments enable row level security;

-- ------------------------------------------------------------------- helpers
-- "Hoy" en hora de la Ciudad de México (la app valida vigencias con esa zona horaria).
create or replace function public.mx_today() returns date
language sql stable set search_path = public as $$
  select (now() at time zone 'America/Mexico_City')::date
$$;

-- Nuevo estado de una cuenta tras comprar un plan.
-- Mensual/anual se suman desde hoy o desde el vencimiento actual (lo que sea mayor), así una
-- renovación anticipada no pierde días. Una cuenta de por vida nunca se degrada.
create or replace function public._plan_after_purchase(
  cur_plan text, cur_lifetime boolean, cur_exp date, bought text,
  out plan_type text, out is_lifetime boolean, out exp_date date
)
language plpgsql stable set search_path = public as $$
declare
  base date := greatest(coalesce(cur_exp, public.mx_today()), public.mx_today());
begin
  if coalesce(cur_lifetime, false) or cur_plan = 'vitalicio' or bought = 'vitalicio' then
    plan_type := 'vitalicio'; is_lifetime := true; exp_date := null;
  elsif bought = 'anual' then
    plan_type := 'anual'; is_lifetime := false; exp_date := (base + interval '1 year')::date;
  else
    plan_type := 'mensual'; is_lifetime := false; exp_date := (base + interval '1 month')::date;
  end if;
end $$;

-- Aplica un pago ya registrado a su cuenta (si existe). Idempotente.
create or replace function public._fulfill_payment(p_payment_id bigint) returns jsonb
language plpgsql set search_path = public as $$
declare
  pay   public.payments;
  usr   record;
  nxt   record;
begin
  select * into pay from public.payments where id = p_payment_id for update;
  if pay.status = 'applied' then
    return jsonb_build_object('status', 'duplicate', 'email', pay.email, 'plan', pay.plan_type, 'exp_date', pay.exp_date_after);
  end if;

  select u.plan_type, u.is_lifetime, u.exp_date into usr
    from public.users u where lower(u.email) = pay.email for update;
  if not found then
    -- Pagó antes de registrarse: queda pendiente y se aplica al crear la cuenta (trigger abajo).
    return jsonb_build_object('status', 'pending', 'email', pay.email, 'plan', pay.plan_type);
  end if;

  select * into nxt from public._plan_after_purchase(usr.plan_type, usr.is_lifetime, usr.exp_date, pay.plan_type);
  update public.users u
     set plan_type = nxt.plan_type, is_lifetime = nxt.is_lifetime, exp_date = nxt.exp_date
   where lower(u.email) = pay.email;
  update public.payments
     set status = 'applied', applied_at = now(), exp_date_after = nxt.exp_date
   where id = pay.id;

  return jsonb_build_object('status', 'applied', 'email', pay.email, 'plan', nxt.plan_type, 'exp_date', nxt.exp_date);
end $$;

-- --------------------------------------------------------------- API (RPC)
-- Registra y aplica un pago de Stripe. Llamarlo varias veces con el mismo stripe_ref no suma de más.
create or replace function public.apply_stripe_payment(
  p_ref             text,
  p_email           text,
  p_plan            text,
  p_subscription_id text   default null,
  p_customer_id     text   default null,
  p_amount          bigint default null,
  p_currency        text   default null,
  p_source          text   default 'webhook'
) returns jsonb
language plpgsql set search_path = public as $$
declare
  v_id bigint;
begin
  if p_ref is null or p_email is null or position('@' in p_email) = 0 then
    raise exception 'apply_stripe_payment: referencia o correo inválido (%, %)', p_ref, p_email;
  end if;
  if p_plan not in ('mensual', 'anual', 'vitalicio') then
    raise exception 'apply_stripe_payment: plan inválido %', p_plan;
  end if;

  insert into public.payments (stripe_ref, email, plan_type, stripe_subscription_id, stripe_customer_id, amount, currency, source)
  values (p_ref, lower(trim(p_email)), p_plan, p_subscription_id, p_customer_id, p_amount, lower(p_currency), p_source)
  on conflict (stripe_ref) do nothing
  returning id into v_id;

  if v_id is null then  -- ya registrado (reintento de Stripe o la app llegó primero)
    select id into v_id from public.payments where stripe_ref = p_ref;
  end if;
  return public._fulfill_payment(v_id);
end $$;

-- Renovación automática de una suscripción (evento invoice.paid de Stripe).
create or replace function public.apply_stripe_renewal(
  p_ref             text,
  p_subscription_id text,
  p_amount          bigint default null,
  p_currency        text   default null
) returns jsonb
language plpgsql set search_path = public as $$
declare
  first_payment public.payments;
begin
  select * into first_payment from public.payments
   where stripe_subscription_id = p_subscription_id
   order by created_at limit 1;
  if not found then
    return jsonb_build_object('status', 'unknown_subscription', 'subscription', p_subscription_id);
  end if;
  return public.apply_stripe_payment(
    p_ref, first_payment.email, first_payment.plan_type, p_subscription_id,
    first_payment.stripe_customer_id, p_amount, p_currency, 'webhook'
  );
end $$;

-- ------------------------------------------- pagos hechos antes de registrarse
create or replace function public.users_apply_pending_payments() returns trigger
language plpgsql set search_path = public as $$
declare
  pay public.payments;
  nxt record;
begin
  for pay in
    select * from public.payments
     where email = lower(new.email) and status = 'pending'
     order by created_at
     for update
  loop
    select * into nxt from public._plan_after_purchase(new.plan_type, new.is_lifetime, new.exp_date, pay.plan_type);
    new.plan_type := nxt.plan_type;
    new.is_lifetime := nxt.is_lifetime;
    new.exp_date := nxt.exp_date;
    update public.payments set status = 'applied', applied_at = now(), exp_date_after = nxt.exp_date where id = pay.id;
  end loop;
  return new;
end $$;

drop trigger if exists users_apply_pending_payments on public.users;
create trigger users_apply_pending_payments
  before insert on public.users
  for each row execute function public.users_apply_pending_payments();

-- ------------------------------------------------------------------ permisos
-- Supabase expone las funciones de "public" por la API: que solo el servidor (service_role) pueda llamarlas.
revoke all on function public.apply_stripe_payment(text, text, text, text, text, bigint, text, text) from public, anon, authenticated;
revoke all on function public.apply_stripe_renewal(text, text, bigint, text)                          from public, anon, authenticated;
revoke all on function public._fulfill_payment(bigint)                                                from public, anon, authenticated;
revoke all on function public._plan_after_purchase(text, boolean, date, text)                         from public, anon, authenticated;
grant execute on function public.apply_stripe_payment(text, text, text, text, text, bigint, text, text) to service_role;
grant execute on function public.apply_stripe_renewal(text, text, bigint, text)                         to service_role;
grant execute on function public._fulfill_payment(bigint)                                               to service_role;
grant execute on function public._plan_after_purchase(text, boolean, date, text)                        to service_role;
grant execute on function public.mx_today()                                                             to service_role;
