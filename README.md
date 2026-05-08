# Bots Trading Autónomos

Sistema de investigación y paper trading para validar estrategias antes de pasar a real. El foco actual está en tres variantes de la familia Fabián y dos bots Polymarket.

> Estado recomendado: **paper trading** hasta tener una muestra estadística suficiente. Trading real desactivado por defecto con `ENABLE_LIVE_TRADING=false`.

## Bots actuales

| Bot | Mercado | Estrategia | Estado actual | Lectura rápida |
|---|---:|---|---|---|
| FabiánPullback (`fabian`) | cTrader / Forex-CFD | Estructura + ruptura + pullback | ON, sin trades cerrados | Falta activar/configurar en cTrader para comparar. |
| Fabian Python (`fabian_py`) | Crypto paper | Port Python de FabiánPullback | ON, 13/13 wins, +$23.7854 total | Mejor resultado actual, pero muestra pequeña. |
| FabianPro (`fabianpro`) | Crypto paper | Fabián + ADX + ATR sizing | ON, 19W/7L, +$12.0897 total | Más robusto, menor win rate, mejor ingeniería de riesgo. |
| PolyKronosPaper (`poly`) | Polymarket UP/DOWN | Kronos + ATR/ADX + Kelly | ON, pérdidas grandes | Problemático; limitar/revisar. |
| PolyPortfolioPaper (`pfolio`) | Polymarket portfolio | Compra/venta cartera | ON, casi plano | Vigilar, no priorizar real todavía. |

## Análisis de los 3 bots Fabián

Datos reales de PostgreSQL a 2026-05-08 08:15 CEST:

| Bot | Cerradas | Wins | Losses | Win rate | PnL total | PnL 24h | PnL 7d | Balance |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| FabiánPullback (`fabian`) | 0 | 0 | 0 | n/a | $0.0000 | $0.0000 | $0.0000 | $100.00 |
| Fabian Python (`fabian_py`) | 13 | 13 | 0 | 100.00% | +$23.7854 | +$7.5554 | +$21.7854 | $106.00 |
| FabianPro (`fabianpro`) | 26 | 19 | 7 | 73.08% | +$12.0897 | +$14.2749 | +$11.6032 | $104.99 |

### Conclusión corta

Sí merece la pena crear/terminar el bot de Fabián para cTrader, pero **no porque vaya a mejorar directamente** a Fabian Python o FabianPro. Merece la pena porque será la versión más fiel al diseño original de Fabián y nos dará una tercera fuente de validación en un mercado distinto.

### ¿Mejorará los otros dos bots?

**Directamente no.** El bot cTrader no hará que los otros dos ganen más por sí solo. Pero sí puede mejorar el sistema si lo usamos como laboratorio:

1. Confirmar si la lógica original de estructura/ruptura/pullback funciona fuera de crypto.
2. Comparar ejecución tipo cTrader contra la simulación Python.
3. Detectar qué filtros sobran o faltan: sesiones, spread, RR mínimo, trailing, break-even.
4. Trasladar mejoras validadas de cTrader a Fabian Python/FabianPro.

### Lectura por bot

#### FabiánPullback / cTrader

Estrategia original:

1. Detectar estructura de mercado con swing highs/lows.
2. Clasificar estructura: alcista, bajista o rango.
3. Exigir ruptura fuerte de nivel estructural.
4. Esperar pullback a zona de entrada.
5. Entrar con SL detrás de estructura y TP por RR mínimo.
6. Gestionar con break-even y trailing si aplica.

Parámetros recomendados actuales para empezar agresivo pero controlado:

- `RiskPercent`: 2.0%
- `MaxTradesPerSession`: 2
- `MaxTradesPerDay`: 3-4
- `MinRR`: 1.2
- `TVFilterEnabled`: false al inicio
- `MaxSpreadPips`: estricto según símbolo

Estado: sin trades cerrados. No hay evidencia todavía para decir que gana o pierde. Necesita configuración manual en cTrader y al menos 50-100 trades paper/demo.

#### Fabian Python

Port de la estrategia Fabián a Python/crypto.

Configuración aproximada:

- `risk_percent`: 2.0
- `max_daily_loss_pct`: 10.0 en modo crypto
- `max_trades_per_session`: 3
- `max_trades_per_day`: 6
- `min_rr`: 1.0
- `swing_lookback`: 2
- `structure_bars`: 60
- `force_body_multiplier`: 1.2
- `max_wick_to_body_ratio`: 2.0

Resultados reales actuales:

- Muy buenos: 13/13 trades ganadores.
- PnL total positivo: +$23.7854.
- Riesgo: muestra pequeña y posible sobreajuste a SOL/ventana reciente.

Recomendación: **no tocar por ahora**. Dejar correr hasta mínimo 50 trades cerrados y revisar drawdown, no solo win rate.

#### FabianPro

Fusión de FabiánPullback con mejoras cuantitativas:

- ADX para evitar rangos.
- ATR para SL/TP adaptativos.
- Gestión tipo cartera/posición.
- Parámetros más permisivos para crypto.

Configuración clave:

- `risk_percent`: 2.0
- `max_daily_loss_pct`: 10.0
- `min_rr`: 1.0
- `use_adx_filter`: true
- `adx_min`: 20.0
- `use_atr_sizing`: true
- `atr_multiplier_sl`: 1.5
- `atr_multiplier_tp`: 2.5

Resultados reales actuales:

- 26 trades cerrados.
- 19 wins / 7 losses.
- Win rate: 73.08%.
- PnL total: +$12.0897.

Recomendación: FabianPro es probablemente el candidato más serio para evolucionar a real porque sacrifica win rate extremo a cambio de filtros y sizing más defendibles.

## Resultados esperados vs reales

| Bot | Esperado | Real actual | Lectura |
|---|---|---|---|
| FabiánPullback cTrader | Validar estrategia original en Forex/CFD con spread real | Sin operaciones cerradas | Pendiente de activar. |
| Fabian Python | Alto win rate en tendencias limpias, pocas señales | 100% WR, +$23.7854 | Excelente, pero muestra pequeña. |
| FabianPro | Menor win rate que Fabian Python, mejor control de riesgo | 73.08% WR, +$12.0897 | Sano y más creíble. |
| PolyKronosPaper | Capturar edge UP/DOWN con Kelly | Pérdidas fuertes | No apto para real ahora. |
| PolyPortfolioPaper | Break-even o leve positivo por cartera | Casi plano, -$2.3729 total | Puede seguir en observación. |

## Requisitos básicos

- macOS probado en Mac mini.
- Python 3.14 con venv.
- PostgreSQL local.
- FastAPI + Uvicorn.
- launchd para procesos automáticos.
- cTrader instalado para FabiánPullback real/demo.

## Dashboard

Dashboard local:

```bash
curl http://127.0.0.1:8787/api/health
open http://127.0.0.1:8787
```

Servicio launchd actual:

```bash
launchctl print gui/$(id -u)/com.bta.dashboard-api
launchctl kickstart -k gui/$(id -u)/com.bta.dashboard-api
```

## Seguridad para trading real

Antes de activar real:

1. Usar claves API sin permiso de retirada.
2. Empezar con testnet/sandbox cuando exista.
3. Activar `ENABLE_LIVE_TRADING=false` hasta la última revisión manual.
4. Riesgo real inicial recomendado: 0.10%-0.25% por trade.
5. Daily loss real inicial: 1%.
6. Máximo 1-2 posiciones abiertas.
7. Logs obligatorios de cada orden, fill, error y balance.

## Ficheros importantes

- `frontend/index.html` — dashboard React.
- `backend/main.py` — API dashboard.
- `fabian_pullback_bot.py` — port Python de FabiánPullback.
- `fabian_pro_bot.py` — versión FabianPro con ADX/ATR.
- `fabian_config*.json` — configs Fabian.
- `scripts/collector.py` — colector a PostgreSQL.
- `scripts/review_risk_every_2h.py` — revisión automática paper.
- `.env` / `.env.template` — variables de entorno y placeholders.
- `instrucciones.md` — montaje desde cero y preparación trading real.
