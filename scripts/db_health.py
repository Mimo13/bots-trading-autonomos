#!/usr/bin/env python3
"""
DB Health Check & Auto-Recovery for bots-trading-autonomos.

Checks PostgreSQL connectivity, attempts recovery if needed,
and writes a shared state file so other services can degrade gracefully.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

RUNTIME = Path('/Users/mimo13/bots-trading-autonomos-runtime')
DB_STATE_FILE = RUNTIME / 'runtime/db_state.json'
RETRY_COUNT = 5
RETRY_DELAY_S = 3
POSTGRES_DATA = '/opt/homebrew/var/postgresql@14'
POSTGRES_BIN = '/opt/homebrew/bin'


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def log(msg: str) -> None:
    log_path = RUNTIME / 'runtime/logs/db_health.log'
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open('a', encoding='utf-8') as f:
        f.write(f"{now_iso()} | {msg}\n")
    print(f"{now_iso()} | {msg}")


def write_state(state: str, detail: str = '') -> None:
    """Write a shared DB state file so collector and services can degrade gracefully."""
    DB_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with DB_STATE_FILE.open('w', encoding='utf-8') as f:
        json.dump({
            'ts': now_iso(),
            'state': state,       # 'ok' | 'degraded' | 'down'
            'detail': detail,
        }, f)


def pg_isready() -> bool:
    try:
        r = subprocess.run(
            [f'{POSTGRES_BIN}/pg_isready'],
            capture_output=True, text=True, timeout=10
        )
        return r.returncode == 0
    except Exception as e:
        log(f"pg_isready failed: {e}")
        return False


def pg_restart() -> bool:
    """Try to restart PostgreSQL."""
    try:
        # Try brew services first (most common on M1 Mac)
        r = subprocess.run(
            ['brew', 'services', 'restart', 'postgresql@14'],
            capture_output=True, text=True, timeout=30
        )
        if r.returncode == 0:
            log("PostgreSQL restart via brew: OK")
            return True

        # Fallback to pg_ctl
        log(f"brew restart failed ({r.stderr.strip()[:200]}), trying pg_ctl...")
        r = subprocess.run(
            [f'{POSTGRES_BIN}/pg_ctl', 'restart', '-D', POSTGRES_DATA, '-m', 'fast'],
            capture_output=True, text=True, timeout=30
        )
        if r.returncode == 0:
            log("PostgreSQL restart via pg_ctl: OK")
            return True

        log(f"pg_ctl restart failed: {r.stderr.strip()[:200]}")
        return False
    except Exception as e:
        log(f"pg_restart exception: {e}")
        return False


def wait_for_db(max_wait_s: int = 30) -> bool:
    """Wait until DB is accepting connections (handles recovery mode)."""
    log(f"Waiting for DB (up to {max_wait_s}s)...")
    for i in range(max_wait_s):
        if pg_isready():
            log(f"DB accepting connections after ~{i+1}s")
            return True
        time.sleep(1)
    log(f"DB not accepting connections after {max_wait_s}s")
    return False


def test_query() -> bool:
    """Run a simple SELECT 1 to verify DB is fully functional."""
    try:
        r = subprocess.run(
            [f'{POSTGRES_BIN}/psql', '-d', 'bots_dashboard', '-c', 'SELECT 1'],
            capture_output=True, text=True, timeout=10
        )
        if r.returncode == 0 and '1' in r.stdout:
            log("DB query OK")
            return True
        log(f"DB query failed: {r.stderr.strip()[:200]}")
        return False
    except Exception as e:
        log(f"DB query exception: {e}")
        return False


def main() -> int:
    log("=== DB Health Check ===")

    if pg_isready():
        if test_query():
            write_state('ok', 'DB healthy')
            log("✅ DB is healthy")
            return 0
        else:
            # pg_isready says yes but query fails (e.g. recovery)
            log("⚠️  pg_isready OK but query failed — DB may be in recovery")
            if wait_for_db(30) and test_query():
                write_state('ok', 'DB recovered from idle/recovery')
                log("✅ DB recovered")
                return 0
            write_state('degraded', 'pg_isready OK but query failing')
            return 1
    else:
        write_state('down', 'pg_isready failed')
        log("❌ DB is DOWN, attempting restart...")

        if pg_restart():
            if wait_for_db(60):
                if test_query():
                    write_state('ok', 'DB restarted and healthy')
                    log("✅ DB restarted successfully")
                    return 0
                else:
                    write_state('degraded', 'DB restarted but queries failing')
                    return 1
            else:
                write_state('degraded', 'DB restart initiated but not yet accepting')
                return 1
        else:
            write_state('down', 'DB restart failed')
            log("❌ DB restart FAILED — manual intervention needed")
            return 1


if __name__ == '__main__':
    raise SystemExit(main())
