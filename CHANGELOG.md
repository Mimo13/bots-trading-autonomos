# Changelog

Todos los cambios relevantes del proyecto `bots-trading-autonomos`.

## [2026-05-06]

### Added
- Dashboard con 2 pestañas (FabiánPullback / PolyKronosPaper) y métricas operativas.
- API FastAPI con endpoints de estado, operaciones, alertas, latencia y estrategia.
- Persistencia PostgreSQL (`bots_dashboard`) con tablas de estado, trades, posiciones, wallet y estrategia.
- Pipeline de estrategia en paper:
  - recomendaciones (`strategy_advisor.py`)
  - A/B simulation (`strategy_ab_sim.py`)
  - decisiones de promoción seguras (`strategy_promote.py`)
- Ciclo automático cada 2h (`strategy_cycle_2h.py`) para revisión y ajuste en paper.
- Servicios launchd autónomos para operar tras reinicio del sistema.
- Documentación operativa detallada en `IMPLEMENTACION_Y_OPERACION.md`.

### Changed
- README actualizado con arquitectura, operación y comandos.
- `tradingview_bridge_cycle.py` robustecido con fallback y estado del bridge.
- Monitor de latencia ampliado con verificación de frescura de collector vía DB.
- Dashboard ampliado con:
  - reason-codes diarios y por hora
  - comparativa semanal bot vs bot
  - tabla de A/B tests
  - tabla de promociones

### Fixed
- Ejecución de scripts de estrategia desde venv del proyecto (evita fallos de módulos).
- Refresh del timestamp de señal fallback cTrader en errores de TradingView.

### Ops
- Bots y supervisión en modo paper, sin activación de real-trading.
- Watchdog agresivo activo para autocuración.
- Ajuste de estrategia automático en paper cada 2h.
