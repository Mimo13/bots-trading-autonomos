# Errors

Command failures and integration errors.

---

## [ERR-20260508-001] path_mount_accent

**Logged**: 2026-05-08T07:55:00+02:00
**Priority**: low
**Status**: pending
**Area**: infra

### Summary
Initial project lookup failed because the user wrote `/Volumes/Almacén/...`, but the actual mounted path is `/Volumes/Almacen/...` without accent.

### Details
When working on bots-trading-autonomos, verify `/Volumes` if the supplied path with accent is missing. The existing project path is `/Volumes/Almacen/Desarrollo/bots-trading-autonomos`.

### Suggested Action
Normalize/check volume names before concluding a project path is absent.

### Metadata
- Source: error
- Related Files: frontend/index.html, backend/main.py
- Tags: path, macos, volumes

---

## [ERR-20260508-002] shell_pipe_heredoc_misuse

**Logged**: 2026-05-08T08:01:00+02:00
**Priority**: low
**Status**: pending
**Area**: infra

### Summary
A curl-to-python JSON parsing command failed because a pipeline was combined with a here-doc; stdin was consumed by the here-doc instead of the curl JSON.

### Suggested Action
For quick API JSON extraction, use `python -c` with `urllib.request`, save curl output to a temp file, or avoid here-docs in piped commands.

### Metadata
- Source: error
- Tags: shell, json, curl

---

## [ERR-20260508-003] openclaw_skill_path_stale_after_update

**Logged**: 2026-05-08T08:20:00+02:00
**Priority**: medium
**Status**: pending
**Area**: tooling

### Summary
The injected GitHub skill path pointed to an older OpenClaw package version and `read` failed with ENOENT.

### Details
Skill location requested by runtime: `~/Library/pnpm/global/5/.pnpm/openclaw@2026.5.4_.../skills/github/SKILL.md`. The current docs context references OpenClaw 2026.5.7, so package-versioned skill paths can become stale after updates.

### Suggested Action
If a skill path fails after an update, continue with direct tools and log the stale skill-path issue. Prefer runtime-provided exact paths, but expect occasional post-update drift.

### Metadata
- Source: error
- Tags: openclaw, skills, update

---

## [ERR-20260508-004] web_search_empty_json

**Logged**: 2026-05-08T08:20:00+02:00
**Priority**: low
**Status**: pending
**Area**: tooling

### Summary
`web_search` failed twice with `Unexpected end of JSON input` while checking exchange fee references.

### Suggested Action
When `web_search` returns malformed/empty provider JSON, retry later or use official docs manually via `web_fetch` if URLs are known. Avoid overstating mutable fee numbers without live verification.

### Metadata
- Source: error
- Tags: web_search, exchange_fees

---

## [ERR-20260508-005] fabian_ghost_open_trades_collector

**Logged**: 2026-05-08T12:00:00+02:00
**Priority**: high
**Status**: fixed
**Area**: collector

### Summary
Fabian Python y FabianPro generaban filas de ENTRADA (pnl=0) en el CSV que el collector insertaba como operaciones abiertas con qty=0. 18 ghosts limpiadas de la BD.

### Root Cause
Los bots Fabian escriben dos filas por operación (entrada + salida). La entrada tiene side=BUY/SHORT, pnl=0, result=''. El collector insertaba ambas filas, pero la de entrada siempre tenía token_qty=0 (porque usaba abs(pnl)=0).

### Fix Aplicado
En `load_fabianpro()` y `load_fabian_py()`: añadir `if pnl == 0 and result == '': continue` antes de insertar. Así solo se guardan las filas de cierre con PnL real.

### Metadata
- Source: user_feedback
- Related Files: scripts/collector.py
- Tags: fabian, ghost_trades, collector

## [ERR-20260511-001] collector_schema_unique_indexes

**Logged**: 2026-05-11T10:03:00+02:00
**Priority**: medium
**Status**: fixed
**Area**: backend

### Summary
Collector failed on `positions_open ... on conflict(bot_name,symbol,side)` because an existing DB table lacked the unique constraint declared in `schema.sql`.

### Error
```text
psycopg.errors.InvalidColumnReference: there is no unique or exclusion constraint matching the ON CONFLICT specification
```

### Context
- Occurred while validating collector after adding FabianSpotLong.
- Root cause: `CREATE TABLE IF NOT EXISTS ... unique(...)` does not retrofit constraints onto older tables.

### Suggested Fix
Use explicit `CREATE UNIQUE INDEX IF NOT EXISTS` statements after table creation for conflict targets that must exist on upgraded databases.

### Metadata
- Reproducible: yes
- Related Files: `sql/schema.sql`, `scripts/collector.py`

---

## [ERR-20260511-002] fabian_impossible_sl_tp

**Logged**: 2026-05-11T12:45:00+02:00
**Priority**: high
**Status**: fixed
**Area**: trading-bot

### Summary
FabianPullback paper results were inflated because SL/TP could be generated on the wrong side of entry, making impossible stop-loss hits count as profitable exits.

### Error
```text
Example long before fix: BUY entry 125.22, SL 125.24999, TP 125.25
Example short before fix: SHORT entry 125.14, SL 125.10001, TP 125.10
```

### Context
- Detected while comparing Fabian Python vs FabianSpotLong.
- Root cause: `build_trade_plan()` used the broken structure level as stop anchor instead of the pullback zone edge.
- Also found `daily_realized_pnl > 0` used for DAILY_LOSS_LIMIT, which should check negative realized PnL.

### Suggested Fix
Use the entry-zone edge as stop anchor, validate risk direction, include qty in logs, and reset/re-simulate paper data after fixing.

### Metadata
- Reproducible: yes
- Related Files: `fabian_pullback_bot.py`, `scripts/collector.py`

---
