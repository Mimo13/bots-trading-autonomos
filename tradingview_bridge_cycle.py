#!/usr/bin/env python3
from pathlib import Path
from datetime import datetime, timezone
import json
from tradingview_bridge import write_ctrader_signal, enrich_polymarket_csv

ROOT = Path('/Volumes/Almacen/Desarrollo/bots-trading-autonomos')

CTRADER_OUT = ROOT / 'runtime/tradingview/ctrader_signal.csv'
POLY_BASE = ROOT / 'runtime/polymarket/polymarket_base_input.csv'
POLY_ENRICHED = ROOT / 'runtime/polymarket/polymarket_input_enriched.csv'
STATUS = ROOT / 'runtime/tradingview/bridge_status.json'


def main() -> None:
    status = {'ts': datetime.now(timezone.utc).isoformat().replace('+00:00','Z'), 'ok': True, 'notes': []}
    try:
        write_ctrader_signal(
            output_csv=CTRADER_OUT,
            ctrader_symbol='EURUSD',
            tv_exchange_symbol='OANDA:EURUSD',
            interval='5m',
        )
    except Exception as e:
        status['ok'] = False
        status['notes'].append(f'ctrader signal fallback: {e}')
        CTRADER_OUT.parent.mkdir(parents=True, exist_ok=True)
        CTRADER_OUT.write_text('timestamp_utc,symbol,recommendation,confidence\n' + f"{status['ts']},EURUSD,NEUTRAL,0.0000\n", encoding='utf-8')

    try:
        if POLY_BASE.exists():
            enrich_polymarket_csv(
                input_csv=POLY_BASE,
                output_csv=POLY_ENRICHED,
                interval='5m',
            )
            print(f'Enriched polymarket csv: {POLY_ENRICHED}')
        else:
            status['notes'].append(f'Skipped polymarket enrichment: base file not found -> {POLY_BASE}')
    except Exception as e:
        status['ok'] = False
        status['notes'].append(f'poly enrich fallback: {e}')

    STATUS.parent.mkdir(parents=True, exist_ok=True)
    STATUS.write_text(json.dumps(status, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
