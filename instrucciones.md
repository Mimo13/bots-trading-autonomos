# Instrucciones de montaje y operación

Guía para montar desde cero el proyecto `bots-trading-autonomos`, preparar paper trading y dejar lista la transición futura a real.

## 0. Principio de seguridad

Nunca activar trading real por defecto.

Valores obligatorios mientras se prueba:

```env
ENABLE_LIVE_TRADING=false
EXCHANGE_SANDBOX=true
REQUIRE_MANUAL_LIVE_APPROVAL=true
```

Las API keys reales deben crearse sin permiso de retirada. Solo lectura + trading, y mejor con whitelist de IP si el exchange lo permite.

## 1. Descargar el repo desde GitHub

```bash
cd /Volumes/Almacen/Desarrollo

git clone https://github.com/Mimo13/bots-trading-autonomos.git
cd bots-trading-autonomos
```

Si ya existe:

```bash
cd /Volumes/Almacen/Desarrollo/bots-trading-autonomos
git pull origin main
```

## 2. Crear entorno Python

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Requisitos actuales mínimos:

- `fastapi`
- `uvicorn`
- `psycopg[binary]`

Si se activan conectores reales, se añadirá una librería tipo `ccxt` o SDK nativo por exchange.

## 3. Configurar PostgreSQL

Crear base local:

```bash
createdb bots_dashboard
```

Crear tablas si no existen. Si hay un fichero SQL en `sql/`, aplicar:

```bash
psql postgresql:///bots_dashboard -f sql/schema.sql
```

Comprobar tablas:

```bash
psql postgresql:///bots_dashboard -c '\dt'
```

Tablas esperadas:

- `bot_status`
- `trades`
- `positions_open`
- `wallet_tokens`
- `strategy_recommendations`
- `strategy_ab_tests`
- `strategy_promotions`

## 4. Variables de entorno

Copiar plantilla:

```bash
cp .env.template .env
```

Editar `.env`:

```bash
nano .env
```

Valores mínimos para paper:

```env
DATABASE_URL=postgresql:///bots_dashboard
ENABLE_LIVE_TRADING=false
EXCHANGE_SANDBOX=true
WATCH_ASSETS=BTCUSDT,SOLUSDT,ETHUSDT,ADAUSDT,DOGEUSDT,LINKUSDT
CANDLE_INTERVAL=5m
CANDLE_LIMIT=500
```

### Binance

Datos que se necesitan:

```env
BINANCE_API_KEY=
BINANCE_API_SECRET=
BINANCE_BASE_URL=https://api.binance.com
BINANCE_TESTNET_BASE_URL=https://testnet.binance.vision
BINANCE_RECV_WINDOW=5000
BINANCE_ACCOUNT_TYPE=spot
BINANCE_DEFAULT_SYMBOLS=SOLUSDT,BTCUSDT,ETHUSDT
BINANCE_ORDER_TYPE=MARKET
BINANCE_TIME_IN_FORCE=GTC
BINANCE_MAX_NOTIONAL_PER_ORDER_USDT=25
```

Permisos API recomendados:

1. Lectura activada.
2. Spot trading activado solo cuando paper esté validado.
3. Futures/margin desactivado al inicio.
4. Withdrawals siempre desactivado.
5. IP whitelist activada si es posible.

### MEXC

Datos que se necesitan:

```env
MEXC_API_KEY=
MEXC_API_SECRET=
MEXC_BASE_URL=https://api.mexc.com
MEXC_ACCOUNT_TYPE=spot
MEXC_DEFAULT_SYMBOLS=SOLUSDT,BTCUSDT,ETHUSDT
MEXC_ORDER_TYPE=MARKET
MEXC_TIME_IN_FORCE=GTC
MEXC_MAX_NOTIONAL_PER_ORDER_USDT=25
```

Permisos API recomendados:

1. Lectura.
2. Spot trading cuando se active real.
3. Retiros desactivados.
4. IP whitelist si está disponible.

## 5. Ejecutar dashboard manualmente

```bash
source .venv/bin/activate
DATABASE_URL=postgresql:///bots_dashboard python -m uvicorn backend.main:app --host 0.0.0.0 --port 8787
```

Probar:

```bash
curl http://127.0.0.1:8787/api/health
curl http://127.0.0.1:8787/api/bots
open http://127.0.0.1:8787
```

## 6. Configurar launchd para dashboard

Crear script runtime, ejemplo:

```bash
mkdir -p /Users/$USER/.bta-run/logs
cat > /Users/$USER/.bta-run/api.sh <<'EOF'
#!/bin/zsh
cd /Users/$USER/bots-trading-autonomos-runtime
DATABASE_URL=postgresql:///bots_dashboard ./.venv/bin/python -m uvicorn backend.main:app --host 0.0.0.0 --port 8787
EOF
chmod +x /Users/$USER/.bta-run/api.sh
```

Crear `~/Library/LaunchAgents/com.bta.dashboard-api.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>com.bta.dashboard-api</string>
<key>ProgramArguments</key><array><string>/bin/zsh</string><string>-lc</string><string>/Users/USUARIO/.bta-run/api.sh</string></array>
<key>RunAtLoad</key><true/>
<key>KeepAlive</key><true/>
<key>StandardOutPath</key><string>/Users/USUARIO/.bta-run/logs/api.out.log</string>
<key>StandardErrorPath</key><string>/Users/USUARIO/.bta-run/logs/api.err.log</string>
</dict></plist>
```

Cargar:

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.bta.dashboard-api.plist
launchctl kickstart -k gui/$(id -u)/com.bta.dashboard-api
launchctl print gui/$(id -u)/com.bta.dashboard-api
```

## 7. Dev vs runtime

En este Mac se usa:

- Desarrollo: `/Volumes/Almacen/Desarrollo/bots-trading-autonomos`
- Runtime: `/Users/mimo13/bots-trading-autonomos-runtime`

Cuando se cambie código en dev y se quiera ejecutar en runtime:

```bash
rsync -av --exclude '.venv' --exclude 'runtime/' --exclude '.git' \
  /Volumes/Almacen/Desarrollo/bots-trading-autonomos/ \
  /Users/mimo13/bots-trading-autonomos-runtime/
```

Después revisar `ROOT` hardcodeado en:

- `backend/main.py`
- `tradingview_bridge_cycle.py`
- `final_paper_runner.py`
- `paper_bot_supervisor.py`
- `aggressive_watchdog.py`
- `scripts/review_risk_every_2h.py`
- `scripts/collector.py`

En runtime debe apuntar a:

```python
ROOT = Path('/Users/mimo13/bots-trading-autonomos-runtime')
```

## 8. Estrategias actuales

### 8.1 FabiánPullback / cTrader

Mercado objetivo:

- Forex/CFD en cTrader.
- Paper/demo primero.

Lógica:

1. Detectar swing highs y swing lows.
2. Identificar estructura alcista o bajista.
3. Esperar ruptura fuerte del nivel estructural.
4. Validar cuerpo de vela grande y mecha razonable.
5. Esperar pullback a zona de ruptura.
6. Entrar con orden pendiente/limit según configuración.
7. SL detrás de estructura.
8. TP por RR mínimo.
9. Break-even a 1R y trailing si se activa.

Parámetros recomendados iniciales:

```text
RiskPercent = 2.0
MaxTradesPerSession = 2
MaxTradesPerDay = 3 o 4
MinRR = 1.2
TVFilterEnabled = false
MaxSpreadPips = bajo y adaptado al símbolo
```

Resultado esperado:

- Menos trades que crypto.
- Mejor calidad de ejecución si el spread está controlado.
- Win rate objetivo inicial: 45%-60% con RR >= 1.2.

Resultado real actual:

- Sin trades cerrados. Falta activar/configurar cTrader.

### 8.2 Fabian Python

Mercado objetivo:

- Crypto spot/futuros simulados usando datos OHLCV.

Lógica:

- Port de FabiánPullback a Python.
- Crypto mode sin sesiones estrictas.
- Swing lookback más corto.
- RR mínimo más permisivo.

Parámetros reales actuales:

```json
{
  "risk_percent": 2.0,
  "max_daily_loss_pct": 10.0,
  "max_trades_per_session": 3,
  "max_trades_per_day": 6,
  "min_rr": 1.0,
  "swing_lookback": 2,
  "structure_bars": 60,
  "force_body_multiplier": 1.2,
  "max_wick_to_body_ratio": 2.0,
  "pending_order_expiry_minutes": 60
}
```

Resultado esperado:

- Alto win rate si el mercado está tendencial.
- Riesgo de sobreajuste en muestras pequeñas.
- Necesita drawdown controlado antes de real.

Resultado real actual:

- 13 operaciones cerradas.
- 13 wins / 0 losses.
- Win rate 100%.
- PnL total +$23.7854.
- PnL 7d +$21.7854.

Decisión:

- No tocar por ahora.
- Esperar mínimo 50 trades cerrados.

### 8.3 FabianPro

Mercado objetivo:

- Crypto paper, candidato más serio para real.

Lógica:

1. Base Fabián: estructura + ruptura + pullback.
2. Filtro ADX para evitar rangos.
3. ATR para SL/TP adaptativos.
4. Sizing por riesgo.
5. Control de entradas por estructura.

Parámetros reales actuales:

```json
{
  "risk_percent": 2.0,
  "max_daily_loss_pct": 10.0,
  "min_rr": 1.0,
  "use_adx_filter": true,
  "adx_period": 14,
  "adx_min": 20.0,
  "use_atr_sizing": true,
  "atr_period": 14,
  "atr_multiplier_sl": 1.5,
  "atr_multiplier_tp": 2.5,
  "max_entries_per_structure": 1
}
```

Resultado esperado:

- Win rate menor que Fabian Python.
- Mejor resiliencia en mercados ruidosos.
- Menor probabilidad de overfit.

Resultado real actual:

- 26 operaciones cerradas.
- 19 wins / 7 losses.
- Win rate 73.08%.
- PnL total +$12.0897.
- PnL 24h +$14.2749.
- PnL 7d +$11.6032.

Decisión:

- Candidato principal para pasar a real con tamaño mínimo cuando alcance muestra suficiente.
- Mantener paper hasta 100 trades o al menos 2-4 semanas.

### 8.4 PolyKronosPaper

Mercado objetivo:

- Polymarket UP/DOWN paper.

Lógica:

- Señal Kronos temporal.
- Edge mínimo.
- ATR/ADX.
- Kelly sizing.

Resultado real actual:

- Balance deteriorado.
- PnL total muy negativo.
- Win rate ~40.9%.

Decisión:

- No pasar a real.
- Limitar o pausar hasta rediseñar.

### 8.5 PolyPortfolioPaper

Mercado objetivo:

- Polymarket portfolio paper.

Lógica:

- Compra tokens cuando hay edge.
- Mantiene cartera.
- Vende cuando señal revierte o aparece salida.

Resultado real actual:

- Casi plano.
- PnL total ligeramente negativo.
- Win rate ~49%.

Decisión:

- Mantener observación.
- No real todavía.

## 9. Mejoras pendientes

### Para Fabián/cTrader

- Activar bot C# en cTrader demo.
- Mapear símbolos exactos: EURUSD, XAUUSD, BTCUSD si broker lo soporta.
- Registrar fills reales, spread, slippage, errores de orden.
- Exportar resultados a `trades` en PostgreSQL.
- Comparar reglas de sesión Londres/NY contra modo crypto 24/7.

### Para Fabian Python

- Añadir comisiones y slippage simulados.
- Añadir filtro de volatilidad mínima/máxima.
- Validar en BTC, ETH, SOL por separado.
- Evitar pasar a real solo por 13/13 wins.

### Para FabianPro

- Añadir reducción automática de riesgo tras 2 losses seguidas.
- Añadir partial take profit real.
- Añadir trailing basado en ATR.
- Comparar ADX mínimo 15/20/25 por A/B testing.

### Para exchanges reales

- Implementar módulo común `exchange_client.py` con interfaz:
  - `get_balance()`
  - `get_symbol_filters()`
  - `get_price()`
  - `place_order()`
  - `cancel_order()`
  - `get_open_orders()`
  - `get_fills()`
- Añadir validación de min notional, tick size y step size.
- Añadir dry-run obligatorio.
- Guardar cada intento de orden y respuesta en PostgreSQL.

## 10. Preparación para trading real por exchange

### Binance

Ventajas:

- Alta liquidez.
- API madura.
- Buen soporte para spot y futures.
- Fees competitivas, normalmente bajas con BNB/VIP, pero hay que verificar tabla actual.

Pendiente:

1. Crear API key sin withdrawals.
2. Activar solo lectura al principio.
3. Probar testnet spot si se usa spot.
4. Implementar filtros de símbolo (`LOT_SIZE`, `MIN_NOTIONAL`, `PRICE_FILTER`).
5. Sincronizar timestamp (`recvWindow`, server time).
6. Empezar con spot, no futures.

### MEXC

Ventajas:

- Muchos pares.
- Buena operabilidad para altcoins.
- Fees a menudo competitivas, revisar tabla oficial actual antes de real.

Pendiente:

1. Crear API key sin withdrawals.
2. Verificar permisos spot trade.
3. Implementar firma MEXC.
4. Validar min notional y precisión.
5. Probar con órdenes mínimas o sandbox si disponible.

### Crypto.com Exchange

Ventajas:

- Marca fuerte y buena app.
- API disponible.

Contras:

- Puede tener comisiones/spreads menos atractivos para bots pequeños.
- Menos conveniente que Binance/OKX/Bybit para automatización intensiva.

Pendiente:

- Añadir claves `CRYPTOCOM_API_KEY` y `CRYPTOCOM_API_SECRET`.
- Implementar autenticación específica.
- Revisar fees actuales y límites.

### CoinEx

Ventajas:

- API sencilla.
- Listados amplios.

Contras:

- Menor liquidez que Binance/OKX en pares principales.

Pendiente:

- Añadir `COINEX_ACCESS_ID` y `COINEX_SECRET_KEY`.
- Implementar límites y precisión.
- Usar solo pares líquidos.

### Exchange recomendado adicional: OKX

Recomendación por bajas comisiones, liquidez y operabilidad API: **OKX**.

Motivo:

- API sólida.
- Spot y derivados.
- Buen nivel de liquidez.
- Fees competitivas, revisar tabla oficial antes de real.
- Requiere passphrase además de key/secret.

Variables ya previstas:

```env
OKX_API_KEY=
OKX_API_SECRET=
OKX_API_PASSPHRASE=
```

Alternativa también razonable: Bybit, especialmente para derivados, pero para el primer real conviene empezar por spot en Binance/OKX/MEXC.

## 11. Preparación cTrader real/demo

Pasos:

1. Instalar cTrader.
2. Crear cuenta demo con el broker.
3. Abrir Automate/cBots.
4. Importar o crear `FabianStructurePullbackBot.cs`.
5. Compilar.
6. Adjuntar a un gráfico por símbolo.
7. Configurar:

```text
RiskPercent = 1.0-2.0 en demo
MaxTradesPerSession = 2
MaxTradesPerDay = 3
MinRR = 1.2
MaxSpreadPips = adaptado al par
TVFilterEnabled = false inicialmente
```

8. Ejecutar solo demo.
9. Exportar operaciones al bridge/CSV.
10. Verificar que el collector las sube a PostgreSQL.
11. Evaluar mínimo 50-100 trades antes de real.

Para real en cTrader:

- Empezar con 0.10%-0.25% de riesgo por trade.
- No activar múltiples símbolos el primer día.
- Confirmar stop loss colocado en servidor.
- Confirmar que el bot no abre más posiciones que el límite.
- Revisar logs tras cada sesión.

## 12. Validación antes de pasar a real

Checklist mínimo:

- [ ] 100 trades paper/demo por bot candidato o 2-4 semanas de ejecución estable.
- [ ] PnL positivo neto incluyendo comisiones y slippage.
- [ ] Drawdown máximo aceptable.
- [ ] No hay errores de collector/API.
- [ ] Dashboard refleja balances/trades correctamente.
- [ ] Kill switch probado.
- [ ] `ENABLE_LIVE_TRADING` sigue false hasta aprobación manual.
- [ ] API keys sin withdrawals.
- [ ] Tamaño de orden mínimo y máximo validado.

## 13. Comandos útiles

Estado dashboard:

```bash
curl http://127.0.0.1:8787/api/health
curl http://127.0.0.1:8787/api/bots | python3 -m json.tool
curl http://127.0.0.1:8787/api/weekly-compare | python3 -m json.tool
```

Estado launchd:

```bash
launchctl print gui/$(id -u)/com.bta.dashboard-api
launchctl kickstart -k gui/$(id -u)/com.bta.dashboard-api
```

Guardar cambios:

```bash
git status
git add README.md instrucciones.md .env .env.template
git commit -m "docs: update trading bot strategy and setup docs"
git push origin main
```
