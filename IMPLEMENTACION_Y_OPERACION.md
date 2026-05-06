# Implementación y operación — bots-trading-autonomos

Fecha: 2026-05-06

## Resumen ejecutivo
Se implementó una plataforma desacoplada de OpenClaw para operar en Paper con supervisión y ajuste continuo:
- Dashboard web con 2 pestañas (FabiánPullback y PolyKronosPaper).
- Backend API (FastAPI) para estado, trades, alertas, latencia y estrategia.
- Persistencia en PostgreSQL (`bots_dashboard`).
- Servicios autónomos por `launchd` (arranque tras reinicio).
- Watchdog agresivo (cada 1 minuto) con reintentos.
- Pipeline de estrategia: recomendaciones, A/B en paper, decisiones de promoción.

## Arquitectura
- **Código fuente principal**: `/Volumes/Almacen/Desarrollo/bots-trading-autonomos`
- **Runtime operativo** (usado por launchd): `/Users/mimo13/bots-trading-autonomos-runtime`
- **Wrappers launchd**: `/Users/mimo13/.bta-run/*.sh`
- **Dashboard**: `http://127.0.0.1:8787`
- **DB**: `postgresql:///bots_dashboard`

## Funcionalidad implementada

### Dashboard
- Estado ON/OFF por bot
- Start/Stop por bot
- Modo Paper/Real visible (Real deshabilitado)
- Saldo, PnL día/semana, valor tokens
- Operaciones históricas con color por lado y PnL
- Cartera de tokens
- Operaciones abiertas
- Alertas operativas
- Latencia de feeds/servicios
- Ranking de reason-codes (24h)
- Reason-codes por hora
- Comparativa semanal bot vs bot
- Recomendaciones de estrategia
- Tabla de A/B tests
- Tabla de decisiones de promoción

### API principal
- `GET /api/health`
- `GET /api/bots/{bot}`
- `POST /api/bots/{bot}/start`
- `POST /api/bots/{bot}/stop`
- `GET /api/alerts`
- `GET /api/latency`
- `GET /api/reasons/{bot}?days=1`
- `GET /api/reasons-hourly/{bot}?days=1`
- `GET /api/weekly-compare`
- `GET /api/strategy/recommendations`
- `POST /api/strategy/run`
- `POST /api/strategy/ab-run`
- `GET /api/strategy/ab-tests`
- `POST /api/strategy/promote`
- `GET /api/strategy/promotions`

### Base de datos
Archivo: `sql/schema.sql`
Tablas:
- `bot_status`
- `trades`
- `positions_open`
- `wallet_tokens`
- `strategy_recommendations`
- `strategy_ab_tests`
- `strategy_promotions`

### Estrategia (paper)
Scripts:
- `scripts/strategy_advisor.py` → recomendaciones
- `scripts/strategy_ab_sim.py` → A/B paper (baseline vs candidate)
- `scripts/strategy_promote.py` → decisión segura de promoción (sin aplicar en real)

## Servicios autónomos (launchd)
Cargados en `~/Library/LaunchAgents/`:
- `com.bta.bridge-5m` (cada 5 min)
- `com.bta.supervisor-2h` (cada 2 h)
- `com.bta.watchdog-1m` (cada 1 min)
- `com.bta.collector-1m` (cada 1 min)
- `com.bta.dashboard-api` (keepalive)
- `com.bta.strategy-daily` (22:15)
- `com.bta.strategy-2h` (cada 2 h, añadido en esta fase)

## Estado operativo actual
- Los bots están en modo Paper.
- El ciclo de bridge/supervisión/watchdog está activo.
- El collector actualiza la DB continuamente.
- El ajuste de estrategia se ejecuta cada 2h (advisor + A/B + decisión + autotune paper).

## Logs clave
- Runtime logs: `runtime/logs/*`
- Wrappers launchd: `/Users/mimo13/.bta-run/logs/*`

## Seguridad y límites
- No se habilitó trading real automático.
- Las promociones son conservadoras y orientadas a paper.
- El switch Real permanece deshabilitado en UI.

## Siguientes mejoras sugeridas
1. Feed estable de operaciones cTrader (CSV/API) para mejorar métricas Fabian.
2. Autoajuste con rollback automático por degradación en paper.
3. Alertas push cuando `latency` entre en `critical` > X minutos.
