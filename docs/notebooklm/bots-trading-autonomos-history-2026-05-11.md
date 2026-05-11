# bots-trading-autonomos — histórico, estado y lecciones

Fecha de captura: 2026-05-11 18:59 CEST
NotebookLM notebook: `bots-trading-autonomos`

## Objetivo del proyecto

Sistema de bots de trading autónomos/paper con dashboard local, runner periódico, collector PostgreSQL y foco en pasar a live solo estrategias reproducibles en spot con exchanges disponibles.

Regla de seguridad clave para live: los bots pueden operar autónomamente dentro de la misma cuenta/exchange, pero nunca deben poder extraer fondos. Permitido: buy/sell/rebalance/cancel/kill-switch same-account. Prohibido: withdrawals, transfers externas, cross-exchange Binance↔MEXC, subaccounts, funding/margin transfers y cualquier salida de fondos.

## Arquitectura actual

- Dev repo: `/Volumes/Almacen/Desarrollo/bots-trading-autonomos`
- Runtime activo: `/Users/mimo13/bots-trading-autonomos-runtime`
- Dashboard API: launchd `com.bta.dashboard-api`, puerto `8787`
- DB: PostgreSQL `bots_dashboard`
- Runner principal: `final_paper_runner.py`
- Collector: `scripts/collector.py`
- Frontend: `frontend/index.html`
- Versión dashboard al crear este documento: `APP_VERSION='1.7.0'`

Nota operativa: los agentes launchd corren desde runtime. Tras modificar dev hay que desplegar/copiar a runtime y reiniciar API cuando toque. No asumir que cambios en dev afectan al dashboard.

## Bots activos al 2026-05-11 18:59

| Bot | Par / exchange | Estrategia | Estado paper actual | Lectura |
|---|---|---|---|---|
| `sol_pb` / SolPullbackBot | SOL, Binance USDC | RSI cooling + EMA pullback + ATR stops | $100, 0 trades tras reset | Comparativa limpia; observar. |
| `fabian_spot_long` / FabianSpotLong | SOL, Binance USDC | FabianPullback long-only spot | $100, 0 trades tras reset | Comparativa principal contra SolPullback. |
| `xrp_grid` / XRP Grid Bot | XRP, Binance USDC | Grid dinámica ATR, spot inventory | cash ~$38.80 + XRP ~$62.05, PnL 7d ~$8.97, WR cerrado 100% | Mantener. Encaja con deseo del usuario de acumular XRP a futuro. Vigilar equity cash+tokens, no solo WR. |
| `bnb_spot_long` / BnbSpotLongBot | BNB/USDC, Binance | FabianSpotLong adaptado a BNB | balance ~$129.94, PnL 7d ~$29.94, 12 trades, WR ~92% | Candidato fuerte inicial. Observar si mantiene edge. |
| `bnb_grid` / BNB Grid Bot | BNB/USDC, Binance | Grid dinámica tipo XRP Grid | cash ~$38.82 + BNB ~$62.01, PnL 7d ~$0.197, WR cerrado 100% | Bot de acumulación tranquila, más que rendimiento puro. |
| `poly` / PolyKronosPaper | Polymarket | Predicción binaria UP/DOWN con edge + ATR/ADX + Kelly | balance ~$103.42, 8 trades, WR ~62%, PnL semana ~$3.42 | Ajustado y reseteado; observar. Edge aún dudoso por histórico. |
| `pfolio` / SOL Portfolio Spot | SOLUSDT, MEXC | RSI portfolio spot conservador | balance ~$99.99, 5 trades cerrados, WR ~20%, PnL semana ~-$0.012 | Observación 24–48h; archivar si sigue plano/negativo. |

## Bots archivados o descartados

- FabiánPullback C# (`fabian`): archivado; código conservado; fuera del dashboard.
- `fabian_py`: buen rendimiento histórico aparente, pero incluye shorts no replicables en spot. Reemplazado conceptualmente por `fabian_spot_long`.
- `fabianpro`: shorts + rendimiento mediocre; archivado.
- `mtfreg`, `boxbr`, `scalp`: no replicables/planos o con shorts; archivados/ocultados.
- `tv_sol`: archivado; usaba señal EURUSD para SOL, feed desactualizado y sin launchd real.
- Fabian live testnet (`fabian_live_pullback`, `fabian_live_pro`): archivado; runner no funcionó, 0 trades, sin scheduling/collector fiable.
- Fabian inventory/no-borrow: experimento de usar SOL propio para simular shorts. No reprodujo rendimiento de Fabian full; fees/exposición lo deterioran. Mantener solo como simulador experimental.

## Histórico de hallazgos importantes

### 2026-05-07 — PolyKronos y Portfolio iniciales

- PolyKronos inicialmente no operaba por filtros ultra-conservadores (`atr_min_ratio` demasiado alto). Al relajar filtros generó trades, pero con WR bajo y pérdidas sistemáticas en varias configuraciones.
- PolyPortfolio funcionaba pero casi plano, con PnL cercano a cero.
- TradingView API fallando podía bloquear bridge/collector; se añadió fallback para escribir señal neutral/timestamp fresco.
- Collector tenía bugs: BUY tratados como WIN/LOSS y `token_qty` hardcodeado en algunos loaders.

### 2026-05-10 — SolPullback y dashboard

- Creado `SolPullbackBot`: pullback SOL/USDT con RSI, ATR, EMA y SMA.
- Backtest inicial: $100 → ~$106.09, 2 trades, 100% WR.
- Dashboard visual rehecho: React en `frontend/index.html`, lightweight-charts, tarjetas por bot, portfolios y comparativas.

### 2026-05-11 — Seguridad live y comparativa spot

- Se definió regla de seguridad fuerte: no withdrawals/extracción por APIs.
- Binance live pilot debe evitar USDT si el usuario no puede adquirirlo cómodamente; preferencia inicial SOL/USDC.
- Se creó `FabianSpotLong`: adaptación spot long-only de Fabian para comparar contra SolPullback sin shorts.
- Reset limpio de comparativa `sol_pb` vs `fabian_spot_long` usando marker `runtime/polymarket/comparison_reset.json`.

### Bug crítico Fabian SL/TP

- Se detectó que Fabian Python/FabianSpotLong generaba entradas con SL/TP invertidos: longs con `sl > entry > tp` y shorts con `tp > entry > sl`, inflando WR al 100%.
- Se corrigió `fabian_pullback_bot.py`: planes de trade ahora respetan dirección válida.
- Validación histórica tras fix en SOLUSDT 5m 2026-01-01→2026-05-11:
  - Fabian full: +$1812, 1008 trades, WR ~79.96%, DD ~8.26%.
  - FabianSpotLong: +$1551, 719 trades, WR ~85.95%, DD ~5.66%.
  - Entradas SL/TP inválidas: 0.
- Importante: Fabian full sigue no apto para spot por shorts; FabianSpotLong sí es candidato spot.

### Fees y tokens

- Se aplicó fee conservador 0.05% por leg como aproximación inicial. Después se comprobó que en Binance, para la cuenta actual, XRP/USDC y BNB/USDC no tienen 0 fee: maker 0.10%, taker 0.095%.
- Dashboard separa cash balance de valor de tokens. Para grid bots, mirar `cash + tokens`, no solo balance cash.

### XRP thesis

- El usuario quiere mantener XRP a futuro y acepta caídas temporales.
- XRP Grid encaja como acumulación + trading alrededor del inventario.
- Recomendación futura: crear bolsa core de XRP intocable y dejar que el bot opere solo con una porción.
- Escenarios lógicos si CLARITY se aprueba y Ripple/XRP gana uso real:
  - Claridad regulatoria sin uso fuerte: ~$2–$3.50.
  - CLARITY + uso institucional real: ~$4–$8 en 6–18 meses.
  - Uso puente/liquidación medible: ~$8–$15.
  - Escenario extremo de ciclo/adopción: $20+; no usar como base.
  - Narrativas $100/$500/$1000 no son escenario lógico de corto/medio plazo sin adopción masiva ya materializada.

### BNB bots

- Se comprobó con API real de Binance que BNB/USDC existe y está TRADING.
- Se creó `BnbSpotLongBot` usando lógica FabianSpotLong sobre BNB/USDC.
- Se creó `BNB Grid Bot` usando lógica grid dinámica tipo XRP Grid.
- Primer run fresco:
  - BnbSpotLongBot: $100 → ~$129.94 neto tras fees collector, 12 trades, 11W/1L, WR ~92%, DD reportado ~2.91%.
  - BNB Grid: equity ~$100.83, cash ~$38.82 + BNB ~$62.01, 5 sells cerrados ganadores, PnL realizado ~$0.197.

## Decisiones actuales

1. Mantener `xrp_grid`: realista en spot y alineado con acumulación XRP.
2. Observar `bnb_spot_long`: candidato fuerte por resultados iniciales, pero necesita 24–48h+ de paper real.
3. Mantener `bnb_grid`: acumulación BNB + microprofits; evaluar por equity total.
4. Mantener `poly` en observación tras ajustes conservadores, pero edge histórico es dudoso.
5. Mantener `pfolio` en observación breve; archivar si sigue plano/negativo.
6. No reactivar bots con shorts para live spot salvo que exista infraestructura legal/técnica de borrow/futures explícitamente aprobada.
7. No mover a live hasta verificar API permissions, withdrawal disabled y límites de riesgo.

## Próximas ideas útiles

- Añadir `--only bot_name` a `final_paper_runner.py` para evitar que un run de prueba toque bots no relacionados.
- Para grid bots (`xrp_grid`, `bnb_grid`), separar inventario core vs inventario operativo.
- Actualizar fee model por exchange/par real: Binance actualmente 0.10% maker / 0.095% taker en XRP/USDC y BNB/USDC para esta cuenta, no 0%.
- Crear snapshots periódicos para NotebookLM con:
  - estado `/api/bots`
  - últimos commits
  - cambios de configs
  - decisiones del usuario
  - resumen de qué funcionó/no funcionó

## Commits recientes relevantes

- `40b740e` docs: log BNB bot rollout learnings
- `48e42e7` feat: add BNB spot long and grid paper bots
- `f98d34d` fix: tune PolyKronos and SOL portfolio paper bots
- `a4e597d` feat: archive unrealistic bots, add exchanges, v1.6.0
- `4a254b2` feat: apply exchange fees and token display
- `ff981a1` feat: add Fabian inventory paper simulator
- `ad542a3` fix: correct Fabian trade simulation
- `1582201` feat: add FabianSpotLong paper comparison
- `c5cdf42` docs: revise swap project safety analysis

