create schema if not exists tracker_private;

create table if not exists tracker_private.portfolios (
    user_id uuid primary key references auth.users(id) on delete cascade,
    holdings jsonb not null default '[]',
    history jsonb not null default '{}',
    chart_cache jsonb not null default '{}'
);

create table if not exists tracker_private.sessions (
    token_hash text primary key,
    user_id uuid not null references auth.users(id) on delete cascade,
    expires_at timestamptz not null
);

create index if not exists tracker_sessions_expiry
    on tracker_private.sessions (expires_at);

alter table tracker_private.portfolios enable row level security;
alter table tracker_private.sessions enable row level security;
revoke all on schema tracker_private from public, anon, authenticated;
revoke all on all tables in schema tracker_private from public, anon, authenticated;
