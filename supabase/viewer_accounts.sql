-- View-only accounts for the public dashboard (see simulators/accounts.py).
-- Run this once in your own Supabase project: Dashboard > SQL Editor > New query > paste > Run.
--
-- The dashboard server reaches this table with its SECRET key (SUPABASE_SECRET_KEY), which
-- bypasses row-level security. Nobody else can: RLS is on and there are deliberately no policies.
-- The table holds only a username and a salted scrypt hash, never a password.

create table if not exists public.viewer_accounts (
  username      text primary key check (username ~ '^[a-z0-9_-]{3,30}$'),
  password_hash text not null,
  created_at    timestamptz not null default now()
);

alter table public.viewer_accounts enable row level security;
revoke all on table public.viewer_accounts from anon, authenticated;
