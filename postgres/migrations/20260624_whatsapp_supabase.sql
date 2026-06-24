-- Make the existing public schema compatible with the WhatsApp bot.
-- Safe to run more than once on Supabase.

begin;

create extension if not exists pgcrypto;

do $$
begin
    create type public.friendship_status as enum ('pending', 'accepted', 'declined', 'blocked');
exception
    when duplicate_object then null;
end
$$;

do $$
begin
    create type public.transaction_status as enum ('pending', 'confirmed', 'rejected', 'reversed');
exception
    when duplicate_object then null;
end
$$;

do $$
begin
    create type public.transaction_direction as enum ('in', 'out');
exception
    when duplicate_object then null;
end
$$;

create or replace function public.set_updated_at()
returns trigger
language plpgsql
as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

create table if not exists public.profiles (
    id uuid primary key default gen_random_uuid(),
    telegram_user_id bigint unique,
    telegram_username text,
    whatsapp_user_id text unique,
    whatsapp_display_name text,
    display_name text,
    default_currency text not null default 'ILS',
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

alter table public.profiles
    add column if not exists telegram_user_id bigint,
    alter column telegram_user_id drop not null,
    add column if not exists telegram_username text,
    add column if not exists whatsapp_user_id text,
    add column if not exists whatsapp_display_name text,
    add column if not exists display_name text,
    add column if not exists default_currency text not null default 'ILS',
    add column if not exists created_at timestamptz not null default now(),
    add column if not exists updated_at timestamptz not null default now();

create unique index if not exists idx_profiles_whatsapp_user_id
    on public.profiles (whatsapp_user_id)
    where whatsapp_user_id is not null;

create index if not exists idx_profiles_whatsapp_display_name
    on public.profiles (whatsapp_display_name);

create table if not exists public.friendships (
    id uuid primary key default gen_random_uuid(),
    user_low uuid not null references public.profiles(id) on delete cascade,
    user_high uuid not null references public.profiles(id) on delete cascade,
    status public.friendship_status not null default 'pending',
    invited_by uuid not null references public.profiles(id) on delete restrict,
    accepted_at timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint friendships_canonical_order_chk check (user_low < user_high),
    constraint friendships_distinct_users_chk check (user_low <> user_high),
    constraint friendships_pair_unique unique (user_low, user_high),
    constraint friendships_inviter_in_pair_chk check (invited_by in (user_low, user_high)),
    constraint friendships_accepted_state_chk check (
        (status = 'accepted' and accepted_at is not null)
        or
        (status <> 'accepted' and accepted_at is null)
    )
);

create table if not exists public.transactions (
    id uuid primary key default gen_random_uuid(),
    friendship_id uuid not null references public.friendships(id) on delete cascade,
    created_by uuid not null references public.profiles(id) on delete restrict,
    direction public.transaction_direction not null,
    amount numeric(14, 2) not null,
    currency text not null,
    note text,
    status public.transaction_status not null default 'pending',
    confirmed_by uuid references public.profiles(id) on delete restrict,
    reverses_transaction_id uuid references public.transactions(id) on delete restrict,
    confirmed_at timestamptz,
    rejected_at timestamptz,
    reversed_at timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table if not exists public.balances (
    id uuid primary key default gen_random_uuid(),
    friendship_id uuid not null references public.friendships(id) on delete cascade,
    currency text not null,
    net_amount numeric(14, 2) not null default 0,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint balances_friendship_currency_unique unique (friendship_id, currency)
);

create table if not exists public.payment_requests (
    id uuid primary key default gen_random_uuid(),
    code text not null unique,
    requester_id uuid not null references public.profiles(id) on delete cascade,
    amount numeric(14, 2) not null,
    currency text not null,
    status text not null default 'pending',
    approved_by uuid references public.profiles(id) on delete set null,
    approved_at timestamptz,
    friendship_id uuid references public.friendships(id) on delete set null,
    transaction_id uuid references public.transactions(id) on delete set null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table if not exists public.remind_log (
    friendship_id uuid not null references public.friendships(id) on delete cascade,
    currency text not null,
    last_remind_at timestamptz not null default now(),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint remind_log_pkey primary key (friendship_id, currency)
);

create table if not exists public.processed_events (
    event_id text primary key,
    processed_at timestamptz not null default now(),
    constraint processed_events_event_id_not_blank_chk check (length(trim(event_id)) > 0)
);

create table if not exists public.user_sessions (
    whatsapp_user_id text primary key,
    state text,
    data jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint user_sessions_whatsapp_user_id_not_blank_chk check (length(trim(whatsapp_user_id)) > 0),
    constraint user_sessions_state_not_blank_chk check (
        state is null or length(trim(state)) > 0
    )
);

create index if not exists idx_friendships_user_low_status
    on public.friendships (user_low, status);

create index if not exists idx_friendships_user_high_status
    on public.friendships (user_high, status);

create index if not exists idx_transactions_friendship_status_created_at
    on public.transactions (friendship_id, status, created_at desc);

create index if not exists idx_transactions_friendship_created_at
    on public.transactions (friendship_id, created_at desc);

create index if not exists idx_transactions_created_by_created_at
    on public.transactions (created_by, created_at desc);

create unique index if not exists idx_transactions_reversal_unique
    on public.transactions (reverses_transaction_id)
    where reverses_transaction_id is not null;

create index if not exists idx_balances_friendship
    on public.balances (friendship_id);

create index if not exists idx_payment_requests_requester_status
    on public.payment_requests (requester_id, status);

create index if not exists idx_payment_requests_status_created_at
    on public.payment_requests (status, created_at desc);

create index if not exists idx_remind_log_last_remind_at
    on public.remind_log (last_remind_at);

drop trigger if exists trg_profiles_set_updated_at on public.profiles;
create trigger trg_profiles_set_updated_at
before update on public.profiles
for each row
execute function public.set_updated_at();

drop trigger if exists trg_friendships_set_updated_at on public.friendships;
create trigger trg_friendships_set_updated_at
before update on public.friendships
for each row
execute function public.set_updated_at();

drop trigger if exists trg_transactions_set_updated_at on public.transactions;
create trigger trg_transactions_set_updated_at
before update on public.transactions
for each row
execute function public.set_updated_at();

drop trigger if exists trg_balances_set_updated_at on public.balances;
create trigger trg_balances_set_updated_at
before update on public.balances
for each row
execute function public.set_updated_at();

drop trigger if exists trg_payment_requests_set_updated_at on public.payment_requests;
create trigger trg_payment_requests_set_updated_at
before update on public.payment_requests
for each row
execute function public.set_updated_at();

drop trigger if exists trg_remind_log_set_updated_at on public.remind_log;
create trigger trg_remind_log_set_updated_at
before update on public.remind_log
for each row
execute function public.set_updated_at();

drop trigger if exists trg_user_sessions_set_updated_at on public.user_sessions;
create trigger trg_user_sessions_set_updated_at
before update on public.user_sessions
for each row
execute function public.set_updated_at();

commit;
