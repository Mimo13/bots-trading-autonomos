create table if not exists bot_status (
  bot_name text primary key,
  is_running boolean not null default false,
  mode text not null default 'paper',
  balance_usd numeric not null default 0,
  pnl_day_usd numeric not null default 0,
  pnl_week_usd numeric not null default 0,
  tokens_value_usd numeric not null default 0,
  updated_at timestamptz not null default now()
);

create table if not exists trades (
  id bigserial primary key,
  bot_name text not null,
  ts timestamptz not null,
  side text not null,
  token_qty numeric not null default 0,
  usd_amount numeric not null default 0,
  pnl_usd numeric,
  result text,
  raw jsonb,
  unique (bot_name, ts, side, usd_amount, token_qty)
);

create table if not exists positions_open (
  id bigserial primary key,
  bot_name text not null,
  symbol text not null,
  side text not null,
  qty numeric not null default 0,
  entry_price numeric,
  mark_price numeric,
  unrealized_pnl_usd numeric default 0,
  updated_at timestamptz not null default now(),
  unique (bot_name, symbol, side)
);

create table if not exists wallet_tokens (
  id bigserial primary key,
  bot_name text not null,
  token text not null,
  amount numeric not null default 0,
  usd_value numeric not null default 0,
  updated_at timestamptz not null default now(),
  unique (bot_name, token)
);

create table if not exists strategy_recommendations (
  id bigserial primary key,
  bot_name text not null,
  ts timestamptz not null default now(),
  summary text not null,
  recommendations jsonb not null,
  confidence numeric not null default 0
);

create table if not exists strategy_ab_tests (
  id bigserial primary key,
  bot_name text not null,
  ts timestamptz not null default now(),
  baseline_pnl numeric,
  candidate_pnl numeric,
  baseline_win_rate numeric,
  candidate_win_rate numeric,
  delta_pnl numeric,
  config_patch jsonb not null,
  notes text
);

create table if not exists strategy_promotions (
  id bigserial primary key,
  bot_name text not null,
  ts timestamptz not null default now(),
  decision text not null,
  reason text not null,
  ab_test_id bigint references strategy_ab_tests(id),
  proposed_patch jsonb,
  applied boolean not null default false
);
