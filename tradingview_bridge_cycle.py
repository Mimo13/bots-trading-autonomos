#!/usr/bin/env python3
from pathlib import Path
from tradingview_bridge import write_ctrader_signal, enrich_polymarket_csv

ROOT = Path('/Volumes/Almacen/Desarrollo/bots-trading-autonomos')

CTRADER_OUT = ROOT / 'runtime/tradingview/ctrader_signal.csv'
POLY_BASE = ROOT / 'runtime/polymarket/polymarket_base_input.csv'
POLY_ENRICHED = ROOT / 'runtime/polymarket/polymarket_input_enriched.csv'


def main() -> None:
    write_ctrader_signal(
        output_csv=CTRADER_OUT,
        ctrader_symbol='EURUSD',
        tv_exchange_symbol='OANDA:EURUSD',
        interval='5m',
    )

    if POLY_BASE.exists():
        enrich_polymarket_csv(
            input_csv=POLY_BASE,
            output_csv=POLY_ENRICHED,
            interval='5m',
        )
        print(f'Enriched polymarket csv: {POLY_ENRICHED}')
    else:
        print(f'Skipped polymarket enrichment: base file not found -> {POLY_BASE}')


if __name__ == '__main__':
    main()
