# Nuevo análisis — Live Trading + Swap Visual seguro

Fecha: 2026-05-11 09:05 CEST
Documento base recuperado: `SWAP_PROJECT.md`

## Decisión principal

El proyecto debe cambiar de enfoque:

- ✅ Los bots podrán hacer movimientos por sí solos: comprar, vender, rebalancear, cerrar posiciones, rotar de un activo a otro y cancelar órdenes.
- ❌ Nunca se podrá extraer nada de las cuentas conectadas por API en los ficheros de entorno.
- ❌ No se implementarán retiros, transferencias externas, transferencias entre exchanges, subaccount transfers ni movimientos hacia wallets.
- ✅ Todo movimiento autónomo debe quedar limitado a operaciones internas de trading dentro del mismo exchange/cuenta.

Esto corrige el punto más peligroso del documento original: la idea de transferir fondos Binance ↔ MEXC. Eso implica permisos de withdrawal/transfer o endpoints equivalentes, por lo que debe quedar fuera del alcance.

---

## 1. Regla de oro de seguridad

> Las API keys pueden leer y operar, pero no pueden retirar ni transferir fondos fuera de la cuenta.

Implementación obligatoria en 3 capas:

### 1.1 Permisos reales de la API key

En Binance, MEXC y cualquier exchange futuro:

- Activar: lectura / spot trading.
- Desactivar: withdrawals.
- Desactivar: internal transfer si el exchange lo separa.
- Desactivar: subaccount transfer, universal transfer, funding transfer, P2P, Earn, margin/futures transfer salvo que se diseñe expresamente.
- Restringir por IP si es posible.

### 1.2 Código sin métodos de retirada

La capa `ExchangeClient` no debe exponer métodos como:

- `withdraw()`
- `transfer()`
- `get_deposit_address()` para uso operativo
- `universal_transfer()`
- `subaccount_transfer()`
- `internal_transfer()`

Si alguna librería externa los trae, se envuelve con un adapter que solo exponga métodos permitidos.

### 1.3 Allowlist de endpoints

Cada cliente debe tener una allowlist explícita de endpoints permitidos.

Ejemplo Binance permitido:

- `GET /api/v3/account`
- `GET /api/v3/ticker/*`
- `GET /api/v3/exchangeInfo`
- `GET /api/v3/openOrders`
- `GET /api/v3/myTrades`
- `POST /api/v3/order`
- `DELETE /api/v3/order`

Bloqueado siempre:

- `/sapi/v1/capital/withdraw/apply`
- `/sapi/v1/capital/deposit/address`
- `/sapi/v1/asset/transfer`
- `/sapi/v1/sub-account/*`
- cualquier endpoint de withdrawal, transfer, wallet out, funding movement o address management.

---

## 2. Qué significa “movimientos por sí solas”

Los bots sí pueden actuar sin confirmación humana operación por operación, pero solo dentro de un marco preaprobado.

Movimientos autónomos permitidos:

1. Comprar un token permitido con USDT/USDC.
2. Vender un token permitido a USDT/USDC.
3. Rotar posición: vender token A y comprar token B dentro del mismo exchange.
4. Rebalancear cartera entre activos permitidos dentro de una misma cuenta.
5. Colocar/cancelar órdenes limit/market/stop si el exchange lo permite.
6. Reducir exposición automáticamente por riesgo.
7. Ejecutar kill-switch automático si se supera pérdida diaria, drawdown o anomalía.

Movimientos autónomos prohibidos:

1. Retirar a wallet externa.
2. Transferir de Binance a MEXC o de MEXC a Binance.
3. Transferir a subcuentas.
4. Mover fondos spot ↔ funding/earn/margin/futures sin diseño específico.
5. Crear/guardar/usar direcciones de retiro.
6. Cualquier acción que haga que los fondos abandonen la cuenta API original.

---

## 3. Cambio sobre el Swap Visual

El `Swap Visual` debe convertirse en un motor de conversión interna, no de transferencias.

### Permitido

- Binance SPK → Binance USDC.
- Binance SOL → Binance USDT.
- MEXC MX → MEXC USDT.
- MEXC USDT → MEXC SOL.

### No permitido

- Binance SPK → MEXC USDC.
- Binance SOL → MEXC SOL.
- Cualquier swap cross-exchange que requiera transferencia.

### UI recomendada

Si el usuario elige exchanges distintos:

```text
Movimiento bloqueado por política de seguridad.
Los fondos no pueden salir de las cuentas conectadas por API.
Puedes hacer swaps internos dentro del mismo exchange, pero las transferencias entre exchanges deben quedar fuera del sistema automático.
```

---

## 4. Nueva arquitectura recomendada

```text
Dashboard
  ├─ Bots Live / Paper
  ├─ Swap Interno
  ├─ Autonomous Policy
  └─ Kill Switch
        │
Backend FastAPI
  ├─ Exchange Gateway seguro
  │    ├─ BinanceSpotSafeClient
  │    └─ MexcSpotSafeClient
  ├─ InternalSwapEngine
  ├─ AutonomousBotExecutor
  ├─ RiskGuard
  ├─ AuditLog
  └─ Policy Store
        │
PostgreSQL
  ├─ bot_status
  ├─ trades
  ├─ live_orders
  ├─ internal_swap_log
  ├─ bot_policy
  └─ risk_events
```

La pieza crítica es `Exchange Gateway seguro`: ningún bot debe llamar directamente a `binance_client.py` o a clientes externos. Todos deben pasar por una capa que valida política, presupuesto, símbolo, endpoint y modo.

---

## 5. Política por bot

Crear tabla/config `bot_policy`:

```json
{
  "bot_name": "sol_pb",
  "enabled": true,
  "mode": "paper|live",
  "exchange": "binance",
  "allowed_symbols": ["SOLUSDT"],
  "quote_assets": ["USDT"],
  "max_position_usd": 50,
  "max_daily_notional_usd": 200,
  "max_daily_loss_usd": 10,
  "max_trades_per_day": 8,
  "allow_market_orders": true,
  "allow_limit_orders": true,
  "allow_stop_orders": true,
  "allow_withdraw": false,
  "allow_external_transfer": false,
  "autonomous": true
}
```

Reglas:

- `allow_withdraw` debe ser siempre `false` y no editable desde UI.
- `allow_external_transfer` debe ser siempre `false` y no editable desde UI.
- `autonomous=true` permite operar sin confirmación humana dentro de límites.
- Cambios a `mode=live`, presupuesto o símbolos permitidos sí requieren confirmación humana.

---

## 6. Endpoints revisados

### Swap interno

```python
POST /api/swap/preview
POST /api/swap/execute
GET  /api/swap/history
```

Validación obligatoria:

- `from_exchange == to_exchange`.
- `from_token != to_token`.
- símbolos soportados por el exchange.
- notional dentro de límites.
- no endpoint de transfer/withdraw implicado.

### Bots live autónomos

```python
POST /api/bots/{bot}/live/enable
POST /api/bots/{bot}/paper
POST /api/bots/{bot}/policy
GET  /api/bots/{bot}/policy
POST /api/bots/{bot}/rebalance/preview
POST /api/bots/{bot}/rebalance/execute
```

El bot no debería tener que pedir confirmación para cada orden si está en live autónomo, pero el motor debe bloquear cualquier acción fuera de policy.

### Seguridad

```python
POST /api/kill-switch
GET  /api/risk/events
GET  /api/live/orders
GET  /api/audit
```

---

## 7. Base de datos nueva

### `live_orders`

```sql
CREATE TABLE live_orders (
  id BIGSERIAL PRIMARY KEY,
  ts TIMESTAMPTZ NOT NULL DEFAULT now(),
  bot_name TEXT NOT NULL,
  exchange TEXT NOT NULL,
  symbol TEXT NOT NULL,
  side TEXT NOT NULL,
  order_type TEXT NOT NULL,
  qty NUMERIC NOT NULL,
  price NUMERIC,
  quote_qty NUMERIC,
  exchange_order_id TEXT,
  status TEXT NOT NULL,
  raw JSONB,
  policy_snapshot JSONB NOT NULL
);
```

### `internal_swap_log`

```sql
CREATE TABLE internal_swap_log (
  id BIGSERIAL PRIMARY KEY,
  ts TIMESTAMPTZ NOT NULL DEFAULT now(),
  actor TEXT NOT NULL, -- user|bot:sol_pb|bot:pfolio
  exchange TEXT NOT NULL,
  from_token TEXT NOT NULL,
  from_qty NUMERIC NOT NULL,
  to_token TEXT NOT NULL,
  to_qty NUMERIC,
  usd_notional NUMERIC,
  fee_usd NUMERIC DEFAULT 0,
  status TEXT NOT NULL,
  steps JSONB,
  error TEXT,
  policy_snapshot JSONB NOT NULL
);
```

### `risk_events`

```sql
CREATE TABLE risk_events (
  id BIGSERIAL PRIMARY KEY,
  ts TIMESTAMPTZ NOT NULL DEFAULT now(),
  severity TEXT NOT NULL,
  bot_name TEXT,
  exchange TEXT,
  event_type TEXT NOT NULL,
  message TEXT NOT NULL,
  raw JSONB
);
```

---

## 8. Riesgos actualizados

| Riesgo | Mitigación |
|---|---|
| Bot defectuoso compra/vende demasiado | límites diarios, max trades, max position, kill-switch |
| API key con withdrawal activado por error | checker de permisos al arrancar + alerta crítica + no exponer endpoints |
| Código futuro añade withdraw sin querer | tests que fallen si aparece `withdraw`, `transfer`, `/sapi/v1/asset/transfer`, `/capital/withdraw` |
| Cross-exchange tentador desde UI | bloquear por diseño: `from_exchange != to_exchange` no ejecuta |
| Slippage | límites de slippage, preferir limit orders cuando proceda |
| Doble ejecución | idempotency key y locks por bot/exchange/symbol |
| Estado parcial en swap interno | audit log, conciliación con balances reales |
| Secretos en repo | `.env` fuera de git, no imprimir claves, no leerlas salvo parser necesario |

---

## 9. Tests imprescindibles

1. Test unitario: ningún cliente seguro tiene métodos `withdraw` ni `transfer`.
2. Test unitario: cualquier endpoint bloqueado lanza `SecurityPolicyError`.
3. Test de swap: Binance SPK → Binance USDC permitido si mismo exchange.
4. Test de swap: Binance SPK → MEXC USDC bloqueado.
5. Test de bot: orden dentro de policy permitida.
6. Test de bot: orden fuera de símbolo permitido bloqueada.
7. Test de bot: notional mayor al límite bloqueado.
8. Test de kill-switch: cancela órdenes abiertas y pausa bots live.
9. Test de auditoría: toda orden live escribe `live_orders`.
10. Test de grep/CI: falla si el código contiene endpoints de withdrawal/transfer en clientes productivos.

---

## 10. Plan de implementación revisado

### Fase 0 — Seguridad primero

- [ ] Definir `SecurityPolicyError`.
- [ ] Crear `SafeExchangeClient` con allowlist de operaciones.
- [ ] Añadir tests anti-withdraw/anti-transfer.
- [ ] Verificar que las API keys reales no tienen permiso de withdrawal.

### Fase 1 — Exchange Gateway

- [ ] Refactorizar `binance_client.py` detrás de `exchange/binance_safe.py`.
- [ ] Crear `exchange/mexc_safe.py` solo con spot read/trade.
- [ ] Crear `exchange/policy.py`.
- [ ] Centralizar validación de símbolos, lot size, min notional y límites.

### Fase 2 — Swap interno

- [ ] Reemplazar concepto de cross-exchange por swap interno same-exchange.
- [ ] Implementar preview/execute con idempotency key.
- [ ] Crear `internal_swap_log`.
- [ ] Frontend `/swap` con bloqueo visible si exchanges difieren.

### Fase 3 — Bots autónomos live

- [ ] Crear `bot_policy`.
- [ ] Añadir executor común para bots live.
- [ ] Migrar primero `SolPullbackBot` como piloto.
- [ ] Activar live autónomo solo con límites bajos.
- [ ] Registrar todo en `live_orders`.

### Fase 4 — RiskGuard + Kill Switch

- [ ] Max daily loss por bot.
- [ ] Max daily notional.
- [ ] Max open exposure.
- [ ] Kill-switch manual y automático.
- [ ] Alertas en dashboard.

### Fase 5 — UI y operación

- [ ] Panel de políticas por bot.
- [ ] Historial de órdenes live.
- [ ] Audit log consultable.
- [ ] Indicadores claros Paper/Live/Autonomous.

---

## 11. Conclusión

El proyecto es viable si se redefine así:

- Los bots serán autónomos para trading y rebalanceo interno.
- El Swap Visual será un swap interno por exchange, no un puente entre cuentas.
- Nunca habrá retiros ni transferencias desde las cuentas conectadas por API.
- La seguridad no dependerá solo de “portarse bien”: debe estar en permisos reales, diseño de interfaces, allowlists, tests y auditoría.

Recomendación: empezar por `SolPullbackBot` en live autónomo con presupuesto pequeño y solo `SOLUSDT`, después extender a otros bots cuando RiskGuard y audit log estén probados.
