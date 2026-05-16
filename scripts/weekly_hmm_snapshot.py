#!/usr/bin/env python3
"""Weekly HMM snapshot builder — run via cron every Monday 08:00 (Europe/Madrid).

Produces hmm/output/hmm_regime_snapshot.json that the orchestrator reads
in shadow mode (hmm_regime.enabled=true).

Usage:
    python scripts/weekly_hmm_snapshot.py [--symbols SOLUSDT,XRPUSDT] [--timeframe 4h]

Requirements (same .venv as Path A/B):
    source .venv_hmm/bin/activate
    pip install -r hmm/requirements.txt
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENV_BIN = ROOT / ".venv_hmm" / "bin" / "python3"

PROVIDER_SCRIPT = ROOT / "hmm" / "hmm_regime_provider.py"


def run() -> int:
    parser = argparse.ArgumentParser(description="Weekly HMM regime snapshot")
    parser.add_argument("--symbols", default="SOLUSDT,XRPUSDT")
    parser.add_argument("--timeframe", default="4h", choices=["1h", "4h", "1d"])
    args = parser.parse_args()

    # Use the isolated venv Python
    import subprocess

    cmd = [
        str(VENV_BIN),
        str(PROVIDER_SCRIPT),
        "--symbols", args.symbols,
        "--timeframe", args.timeframe,
        "--force-retrain",
    ]
    result = subprocess.run(cmd, cwd=str(ROOT))
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(run())