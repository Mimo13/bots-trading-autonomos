# Proyecto Live Trading + Swap Visual

Integración de trading real en Binance y MEXC dentro del Dashboard existente.

---

## 1. Arquitectura General

```
┌─────────────────────────────────────────────────────┐
│                    Dashboard                         │
│  ┌─────────┐  ┌──────────────┐  ┌────────────────┐  │
│  │ Bots    │  │ Swap Visual  │  │ Config/Admin   │  │
│  │ Live    │  │ (intercambio)│  │ (whitelist)    │  │
│  └────┬────┘  └──────┬───────┘  └───────┬────────┘  │
│       │              │                  │           │
└───────┼──────────────┼──────────────────┼───────────┘
        │              │                  │
┌───────▼──────────────▼──────────────────▼───────────┐
│               Backend (FastAPI)                      │
│  ┌──────────────┐  ┌──────────┐  ┌──────────────┐   │
│  │ Exchange     │  │ Swap     │  │ Whitelist    │   │
│  │ Abstraction  │  │ Engine   │  │ Management   │   │
│  └──────┬───────┘  └──────────┘  └──────────────┘   │
│         │                                            │
└─────────┼────────────────────────────────────────────┘
          │
     ┌────┴────┐
     │  DB     │
     │(históri-│
     │  co)    │
     └─────────┘

          ┌──────────────────┐
          │  Exchange Layer  │
          ├────────┬─────────┤
          │ Binance│  MEXC   │
          │ Client │  Client │
          └────────┴─────────┘
```

---

## 2. Componentes del Proyecto

### 2.1 Exchange Abstraction Layer

**Archivo:** `exchange/base.py` + `exchange/binance.py` + `exchange/mexc.py`

Capa común para ambos exchanges con interfaz unificada:

```python
class ExchangeClient(ABC):
    @abstractmethod
    def get_balance(self) -> List[Balance]:
    @abstractmethod
    def get_ticker(self, symbol: str) -> Ticker:
    @abstractmethod
    def place_market_order(self, symbol, side, qty) -> Order:
    @abstractmethod
    def get_filters(self, symbol) -> FilterInfo:
    @abstractmethod
    def get_open_orders(self, symbol) -> List[Order]:
    @abstractmethod
    def cancel_order(self, symbol, order_id) -> bool:
```

Intercambio de tokens (swap) se implementa como:
- **VENDER** token A por USDT (market order)
- **COMPRAR** token B con USDT (market order)
- Transacción atómica log: registro completo en DB

**Lot size validation** automática: cada exchange tiene reglas distintas (LOT_SIZE, MIN_NOTIONAL). El engine calcula el qty redondeado correctamente.

### 2.2 Whitelist System

**Archivo:** `exchange/whitelist.json` + endpoints en backend

```json
{
  "exchanges": {
    "binance": {
      "name": "Binance",
      "enabled": true,
      "allow_swap": true,
      "allow_bot_trading": true,
      "allow_withdraw": false,
      "api_key_label": "Binance API (lectura+trading)"
    },
    "mexc": {
      "name": "MEXC",
      "enabled": true,
      "allow_swap": true,
      "allow_bot_trading": true,
      "allow_withdraw": false,
      "api_key_label": "MEXC API (lectura+trading)"
    }
  },
  "external_addresses": [],  // Siempre vacío - no se permiten transfers externas
  "max_daily_swap_usd": 500,
  "max_swap_percent": 50,    // % máximo del balance de un token por swap
  "require_confirmation": true,
  "audit_log": true
}
```

**Reglas de seguridad:**
- ❌ No se puede retirar a direcciones externas
- ❌ No se puede enviar a wallets que no estén en la whitelist
- ✅ Solo transferencias entre Binance ↔ MEXC
- ✅ Si se añade un nuevo exchange, se añade automáticamente a la whitelist
- ✅ Cada swap requiere confirmación explícita
- ✅ Límite diario de USD intercambiado configurable
- ✅ Log de auditoría obligatorio (timestamp, who, what, from, to, amount)

### 2.3 Swap Visual — Frontend

**Nueva página en el Dashboard:** `/swap`

Layout:
```
┌──────────────────────────────────────────────────┐
│  Intercambio de Tokens                           │
├──────────────────────────────────────────────────┤
│  [De: ▼ Selector Exchange]  [Token ▼]  [Saldo]  │
│         ↓                                        │
│  [A:  ▼ Selector Exchange]  [Token ▼]  [Saldo]  │
│                                                   │
│  Cantidad: [___________]  [Max]                   │
│                                                   │
│  💰 Recibirás ≈ XX.XX USDC                       │
│  📊 Precio: 1 SPK = 0.0386 USDT                  │
│  ⚠️ Comisión estimada: 0.1%                      │
│                                                   │
│  ┌──────────────────────────────────────┐        │
│  │  🔄 Intercambiar                     │        │
│  └──────────────────────────────────────┘        │
│                                                   │
│  📋 Historial de intercambios                    │
│  ┌──────┬────────┬──────┬──────┬──────┬──────┐  │
│  │ Fecha│ Desde  │ Token│ A     │Token  │ USD  │  │
│  ├──────┼────────┼──────┼──────┼──────┼──────┤  │
│  │ ...  │ Binance│ SPK  │MEXC  │ USDC │$22.50│  │
│  └──────┴────────┴──────┴──────┴──────┴──────┘  │
└──────────────────────────────────────────────────┘
```

**Casos de uso:**
1. Mismo exchange (Binance SPK → Binance USDC): swap interno
2. Entre exchanges (Binance SPK → MEXC USDC): require transferencia
3. Mismo token entre exchanges (Binance SOL → MEXC SOL): transferencia directa

**Modal de confirmación:**
```
┌──────────────────────────────────────┐
│  ⚠️ Confirmar intercambio            │
│                                      │
│  Vas a intercambiar:                 │
│  602 SPK ($23.22) en Binance →       │
│  ≈23.10 USDC en MEXC                 │
│                                      │
│  Esto ejecutará:                     │
│  1. Vender 602 SPK por USDT          │
│  2. Transferir USDT a MEXC           │
│                                      │
│  [Cancelar]  [✅ Confirmar]          │
└──────────────────────────────────────┘
```

### 2.4 Live Trading para Bots

Cada bot en el dashboard tendrá un toggle:

```
┌──────────────────────────────────────────┐
│  SolPullbackBot                 ⚙️       │
│  Balance: $106.09  Acierto: 100%        │
│  ┌──────────────────────────────────┐   │
│  │  Modo: ○ Paper  ● Live (Binance) │   │
│  │  Risk: ████████░░ 20%           │   │
│  │  Status: 🟢 Running              │   │
│  └──────────────────────────────────┘   │
└──────────────────────────────────────────┘
```

**Pipeline live vs paper:**

```
Paper (actual):  Bot → Simula → Escribe CSV → Collector → DB
Live (nuevo):    Bot → Exchange.place_market_order() → DB

El bot recibe un parámetro --live que en lugar de 
simular, ejecuta órdenes reales en el exchange.
```

**Sistema de safety:**
- Modo Paper por defecto (ya existe)
- Toggle Live requiere confirmación + 2FA (opcional)
- Max loss diario configurable
- Kill switch global: un endpoint `/api/kill-switch` que cancela todas las órdenes abiertas
- Notificación cada vez que un bot pasa a Live
- Log de cada orden real con confirmation del exchange

### 2.5 Endpoints Nuevos en Backend

```python
# ── Intercambio de tokens ─────────────────────
POST /api/swap/preview
  Body: {"from_exchange":"binance","from_token":"SPK",
         "to_exchange":"binance","to_token":"USDC","amount":602}
  Resp: {"ok":true,"from":{"asset":"SPK","qty":602,"usd":23.22},
           "to":{"asset":"USDC","qty":23.10,"usd":23.10},
           "fee_usd":0.023,"steps":["sell","buy"]}

POST /api/swap/execute
  Body: {"id":"swap_xxx","confirm":true}
  Resp: {"ok":true,"txid":"...","steps":[
           {"action":"sell","symbol":"SPKUSDT","qty":602,"order_id":"..."},
           {"action":"buy","symbol":"USDCUSDT","qty":23.1,"order_id":"..."}
         ]}

GET /api/swap/history
  Resp: {"swaps":[
           {"ts":"...","from_exchange":"binance","from_token":"SPK",
            "to_exchange":"binance","to_token":"USDC",
            "qty":602,"usd":23.22,"status":"completed"}
         ]}

# ── Live mode ────────────────────────────────
POST /api/bots/{bot}/live
  Body: {"exchange":"binance","symbol":"SOLUSDT","confirm":true}
  Resp: {"ok":true,"mode":"live","balance":106.09}

POST /api/bots/{bot}/paper
  Body: {"confirm":true}
  Resp: {"ok":true,"mode":"paper"}

# ── Kill Switch ──────────────────────────────
POST /api/kill-switch
  Body: {"confirm":true}
  Resp: {"ok":true,"cancelled_orders":3,"bots_stopped":["sol_pb","fabianpro"]}

# ── Whitelist ────────────────────────────────
GET /api/admin/whitelist
POST /api/admin/whitelist/exchange
DELETE /api/admin/whitelist/exchange
# (solo accessible desde localhost, requiere confirmación extra)
```

---

## 3. Plan de Implementación por Fases

### Fase 1 — Base (día 1)
- [ ] `exchange/base.py`: abstract base class
- [ ] `exchange/binance.py`: mover `binance_client.py` a la nueva estructura + refactorizar
- [ ] `exchange/mexc.py`: crear cliente MEXC con misma interfaz
- [ ] `exchange/whitelist.json`: archivo de configuración
- [ ] Tests básicos de conexión (ping, balance)

### Fase 2 — Swap Engine (día 2)
- [ ] `exchange/swap_engine.py`: motor de intercambio
  - [ ] Calcular ruta: directa (mismo exchange) o transferencia (entre exchanges)
  - [ ] Validar lot sizes, min notional
  - [ ] Preview: calcular recibirás, fees, slippage estimado
- [ ] Endpoints: `/api/swap/preview`, `/api/swap/execute`, `/api/swap/history`
- [ ] Log de auditoría en DB (tabla `swap_log`)
- [ ] Modal de confirmación

### Fase 3 — Frontend Swap (día 2-3)
- [ ] Nueva página `/swap` en React
- [ ] Selectores de exchange + token con saldos en tiempo real
- [ ] Preview del intercambio (precio, fees, recibirás)
- [ ] Modal de confirmación
- [ ] Historial de intercambios
- [ ] Página Admin para whitelist

### Fase 4 — Live Trading (día 3-4)
- [ ] Modificar `final_paper_runner.py` para aceptar `--live`
- [ ] Toggle Paper/Live por bot en frontend
- [ ] Kill switch global
- [ ] Safety limits (max daily loss, stop on DD > N%)
- [ ] Notificaciones de estado live

### Fase 5 — Seguridad y tests (día 4-5)
- [ ] Confirmación obligatoria para todo lo que mueva dinero
- [ ] Test en testnet primero (ya existe)
- [ ] Rate limiting en endpoints sensibles
- [ ] Verificar que no se puedan retirar a direcciones externas
- [ ] Log de auditoría completo y consultable

---

## 4. Seguridad — Reglas de Oro

| Regla | Implementación |
|-------|---------------|
| ❌ No transfers a direcciones externas | Whitelist vacía de direcciones. Cualquier transferencia requiere exchange en whitelist |
| ✅ Solo Binance ↔ MEXC entre sí | Solo exchanges registrados en whitelist pueden recibir/enviar |
| ✅ Confirmación siempre | Cada swap requiere tap "Confirmar" en el modal |
| ✅ Límite diario | `max_daily_swap_usd` configurable en whitelist.json |
| ✅ Log imborrable | Tabla `swap_log` en PostgreSQL, INSERT-only |
| ✅ Kill switch | Endpoint que para todos los bots y cancela órdenes abiertas |
| ✅ Modo paper por defecto | Los bots arrancan en paper hasta que se activa Live explícitamente |
| ✅ API keys seguras | En `.env.local` fuera del repo, como ya están |

---

## 5. Diagrama de Flujo — Swap

```
Usuario:  Selecciona SPK → USDC en UI
            │
            ▼
Frontend:  GET /api/swap/preview
  Body: {"from_exchange":"binance","from_token":"SPK",
         "to_exchange":"mexc","to_token":"USDC","amount":602}
            │
            ▼
Backend:   SwapEngine.preview()
  1. Verificar whitelist (binance y mexc permitidos)
  2. Obtener precio SPK/USDT en Binance
  3. Obtener precio USDC/USDT en MEXC (≈1.0)
  4. Calcular recibirás = (602 * precio) - fees
  5. Validar límites diarios
  6. Devolver preview con desglose
            │
            ▼
Frontend:  Muestra preview + botón "Confirmar"
            │
Usuario:   Toca Confirmar
            │
            ▼
Frontend:  POST /api/swap/execute
  Body: {"id":"preview_id","confirm":true}
            │
            ▼
Backend:   SwapEngine.execute()
  1. 🔒 Lock (evitar doble ejecución)
  2. Vender 602 SPK por USDT en Binance (market order)
  3. Transferir USDT a MEXC (deposit address de whitelist)
  4. Comprar USDC con USDT en MEXC (market order)
  5. Log en swap_log
  6. Devolver resultado
            │
            ▼
Frontend:  ✅ "Intercambio completado"
           Muestra: 602 SPK → 23.10 USDC
           TX IDs: Binance: xxx, MEXC: yyy
```

---

## 6. Estructura de Archivos

```
backend/
  main.py                  ← añadir endpoints nuevos
  exchange/
    __init__.py
    base.py                ← AbstractExchangeClient
    binance.py             ← BinanceClient (migrado de binance_client.py)
    mexc.py                ← MexcClient (nuevo)
    swap_engine.py         ← SwapEngine (lógica de intercambio)
    whitelist.json         ← Config de seguridad
    errors.py              ← ExchangeError, InsufficientBalance, etc.
  scripts/
    collector.py           ← añadir swap_log al collector
  frontend/
    index.html             ← añadir página /swap, modals, toggles live
```

---

## 7. Tabla swap_log en DB

```sql
CREATE TABLE swap_log (
  id SERIAL PRIMARY KEY,
  ts TIMESTAMPTZ NOT NULL DEFAULT now(),
  from_exchange TEXT NOT NULL,
  from_token TEXT NOT NULL,
  from_qty NUMERIC NOT NULL,
  from_usd NUMERIC NOT NULL,
  to_exchange TEXT NOT NULL,
  to_token TEXT NOT NULL,
  to_qty NUMERIC NOT NULL,
  to_usd NUMERIC NOT NULL,
  fee_usd NUMERIC DEFAULT 0,
  steps JSONB,           -- detalle de cada orden ejecutada
  status TEXT NOT NULL DEFAULT 'pending',  -- pending, completed, failed
  error TEXT,
  confirmed_by TEXT DEFAULT 'user'
);

CREATE INDEX idx_swap_log_ts ON swap_log(ts DESC);
```

---

## 8. Riesgos y Mitigaciones

| Riesgo | Mitigación |
|--------|-----------|
| Slippage en market orders | Preview con slippage estimado; opción de limit orders |
| Error de conexión con exchange | Retry lógica + rollback parcial (si vendiste pero no compraste, alerta) |
| Doble click en Confirmar | Lock por idempotency key (el preview_id es single-use) |
| API key expirada | Test de conexión al arrancar + alerta en dashboard |
| Bug en bot live | Kill switch manual + max daily loss automático + modo paper recovery |
| Transferencia entre exchanges falla | Log del paso donde falló; estado "partial" con instrucciones manuales |

---

## 9. Resumen de Requisitos

| Funcionalidad | Prioridad | Tiempo estimado |
|--------------|-----------|-----------------|
| Swap visual SPK→USDC | 🔴 Alta | 2 días |
| Exchange Abstraction Layer | 🔴 Alta | 1 día |
| Live trading bots | 🟡 Media | 2 días |
| Whitelist + seguridad | 🔴 Alta | 1 día |
| Frontend swap page | 🔴 Alta | 1 día |
| Log de auditoría | 🟡 Media | 0.5 día |
| Kill switch | 🟡 Media | 0.5 día |
| Tests + validaciones | 🟢 Baja | 1 día |

**Total estimado: 5-6 días** si trabajo enfocado.

---

*Documento vivo — los requisitos pueden ajustarse durante la implementación.*
