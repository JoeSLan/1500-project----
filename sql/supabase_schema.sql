-- LegalSite Auditor — tabla de cuentas.
-- Ejecuta este script completo en Supabase → SQL Editor → New query → Run.
-- Es idempotente: sirve tanto si la tabla no existe como si ya la creaste con otras columnas.

create table if not exists public.users (
  id uuid primary key default gen_random_uuid()
);

alter table public.users add column if not exists email         text;
alter table public.users add column if not exists password_hash text;
alter table public.users add column if not exists name          text;
alter table public.users add column if not exists plan_type     text    not null default 'mensual';
alter table public.users add column if not exists exp_date      date;
alter table public.users add column if not exists is_active     boolean not null default true;
alter table public.users add column if not exists is_lifetime   boolean not null default false;
alter table public.users add column if not exists created_at    timestamptz not null default now();
alter table public.users add column if not exists updated_at    timestamptz not null default now();

-- Correo único y siempre en minúsculas (la app lo normaliza antes de guardar).
create unique index if not exists users_email_key on public.users (lower(email));

do $$
begin
  if not exists (select 1 from pg_constraint where conname = 'users_plan_type_check') then
    alter table public.users
      add constraint users_plan_type_check check (plan_type in ('mensual', 'anual', 'vitalicio'));
  end if;
end $$;

-- updated_at automático al editar una fila (renovaciones, bajas).
create or replace function public.users_touch_updated_at() returns trigger
language plpgsql as $$
begin
  new.updated_at = now();
  return new;
end $$;

drop trigger if exists users_touch_updated_at on public.users;
create trigger users_touch_updated_at
  before update on public.users
  for each row execute function public.users_touch_updated_at();

-- SEGURIDAD: RLS activado y SIN políticas públicas.
-- Así nadie puede leer los password_hash con la anon key; la app usa la service_role key
-- guardada en Streamlit Secrets (solo vive en el servidor, nunca llega al navegador).
alter table public.users enable row level security;

-- ---------------------------------------------------------------------------
-- Operaciones manuales frecuentes (ejecútalas cuando un cliente pague):
--
-- Renovar 1 mes:   update public.users set plan_type='mensual', is_active=true,
--                    exp_date = greatest(coalesce(exp_date, current_date), current_date) + interval '1 month'
--                  where email = 'cliente@correo.com';
-- Renovar 1 año:   update public.users set plan_type='anual', is_active=true,
--                    exp_date = greatest(coalesce(exp_date, current_date), current_date) + interval '1 year'
--                  where email = 'cliente@correo.com';
-- De por vida:     update public.users set plan_type='vitalicio', is_lifetime=true, is_active=true, exp_date=null
--                  where email = 'cliente@correo.com';
-- Dar de baja:     update public.users set is_active=false where email = 'cliente@correo.com';
-- ---------------------------------------------------------------------------
