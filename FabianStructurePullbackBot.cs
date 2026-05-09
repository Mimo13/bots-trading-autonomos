// --------------------------------------------------------------------------------
// FabianStructurePullbackBot v2 — cTrader cBot
// Estrategia: estructura de mercado → ruptura fuerte → pullback → entrada
//
// Versión mejorada con parámetros agresivos validados en paper:
//   - SwingLookback=1, StructureBars=40, ForceBody=1.0
//   - EntryMode=1 (conservative edge), slBufferPips=0.1
//   - Risk 3%, MaxTrades 8/día, MinRR 1.0
//   - Trailing + break even desde 1R
//
// Basado en fabian_pullback_bot.py (Python) — mantener sincronizado.
// --------------------------------------------------------------------------------
using System;
using System.Collections.Generic;
using System.Linq;
using cAlgo.API;
using cAlgo.API.Indicators;

namespace cAlgo.Robots
{
    [Robot(TimeZone = TimeZones.UTC, AccessRights = AccessRights.None)]
    public class FabianStructurePullbackBot : Robot
    {
        // ========================================================================
        // PARÁMETROS EDITABLES DESDE LA UI DE cTRADER
        // ========================================================================

        [Parameter("Volume (lots)", Group = "Gestión de Riesgo", DefaultValue = 0.01)]
        public double VolumeLots { get; set; } = 0.01;

        [Parameter("Risk Percent", Group = "Gestión de Riesgo", DefaultValue = 3.0)]
        public double RiskPercent { get; set; } = 3.0;

        [Parameter("Use Risk% Sizing", Group = "Gestión de Riesgo", DefaultValue = true)]
        public bool UseRiskSizing { get; set; } = true;

        [Parameter("Max Trades Per Day", Group = "Gestión de Riesgo", DefaultValue = 8)]
        public int MaxTradesPerDay { get; set; } = 8;

        [Parameter("Max Trades Per Session", Group = "Gestión de Riesgo", DefaultValue = 4)]
        public int MaxTradesPerSession { get; set; } = 4;

        [Parameter("Min RR", Group = "Gestión de Riesgo", DefaultValue = 1.0)]
        public double MinRR { get; set; } = 1.0;

        [Parameter("Max Daily Loss %", Group = "Gestión de Riesgo", DefaultValue = 10.0)]
        public double MaxDailyLossPercent { get; set; } = 10.0;

        [Parameter("SL Buffer (pips)", Group = "Gestión de Riesgo", DefaultValue = 0.1)]
        public double SlBufferPips { get; set; } = 0.1;

        [Parameter("Swing Lookback (barras)", Group = "Estructura", DefaultValue = 1)]
        public int SwingLookback { get; set; } = 1;

        [Parameter("Structure Bars (lookback)", Group = "Estructura", DefaultValue = 40)]
        public int StructureBars { get; set; } = 40;

        [Parameter("Body Avg Period", Group = "Estructura", DefaultValue = 10)]
        public int BodyAvgPeriod { get; set; } = 10;

        [Parameter("Force Body Multiplier", Group = "Ruptura", DefaultValue = 1.0)]
        public double ForceBodyMultiplier { get; set; } = 1.0;

        [Parameter("Max Wick/Body Ratio", Group = "Ruptura", DefaultValue = 3.0)]
        public double MaxWickToBodyRatio { get; set; } = 3.0;

        [Parameter("Entry Mode", Group = "Entrada", DefaultValue = 1)]
        public int EntryMode { get; set; } = 1; // 0=midpoint, 1=conservative_edge

        [Parameter("Pending Order Expiry (bars)", Group = "Entrada", DefaultValue = 12)]
        public int PendingOrderExpiry { get; set; } = 12; // ~12 barras 5m = 60min

        [Parameter("Enable Trailing", Group = "Gestión de Salida", DefaultValue = true)]
        public bool EnableTrailing { get; set; } = true;

        [Parameter("Enable Break Even at 1R", Group = "Gestión de Salida", DefaultValue = true)]
        public bool EnableBreakEvenAt1R { get; set; } = true;

        [Parameter("Crypto Mode (24/7)", Group = "Sesiones", DefaultValue = true)]
        public bool CryptoMode { get; set; } = true;

        [Parameter("Avoid First Minutes", Group = "Sesiones", DefaultValue = 0)]
        public int AvoidFirstSessionMinutes { get; set; } = 0;

        // ========================================================================
        // VARIABLES INTERNAS
        // ========================================================================

        private double _peakBalance;
        private double _dailyStartEquity;
        private double _dailyPnl;
        private int _tradesToday;
        private int _tradesLondon, _tradesNY;
        private int _consecutiveLosses;
        private int _pauseUntilBar;
        private int _totalWins, _totalLosses, _totalTrades;
        private double _currentBodyAvg;
        private int _lastResetDay = -1;
        private readonly Dictionary<string, int> _orderEntryBars = new Dictionary<string, int>();

        // ========================================================================
        // ON START
        // ========================================================================

        protected override void OnStart()
        {
            _peakBalance = Account.Equity;
            _dailyStartEquity = Account.Equity;

            Print("=== FabianPullback v2 (config agresiva) ===");
            Print($"Risk {RiskPercent}% | RR {MinRR} | SL buf {SlBufferPips}p | MaxD {MaxTradesPerDay}/día");
            Print($"Swing={SwingLookback} | StructB={StructureBars} | BodyAvg={BodyAvgPeriod}");
            Print($"ForceBody={ForceBodyMultiplier} | WickRatio={MaxWickToBodyRatio}");
            Print($"EntryMode={EntryMode} | Crypto={CryptoMode} | Trailing={EnableTrailing}");
        }

        // ========================================================================
        // ON BAR — núcleo de la estrategia
        // ========================================================================

        protected override void OnBar()
        {
            int idx = Bars.Count - 1;
            if (idx < BodyAvgPeriod + SwingLookback * 2)
                return; // Calentamiento inicial

            // 1. Limpiar órdenes expiradas
            CleanExpiredOrders(idx);

            // 2. Calcular tamaño medio de cuerpo
            ComputeBodyAverage(idx);

            // 3. Reset diario si cambia el día
            ResetDailyIfNeeded(idx);

            // 4. Pausa por racha de pérdidas
            if (_pauseUntilBar > idx)
                return;

            // 5. Límites diarios
            if (!CanTrade(idx))
                return;

            // 6. Swing highs / lows (con lookback=1 detecta más señales)
            var swingHighs = FindSwingHighs(idx);
            var swingLows = FindSwingLows(idx);
            if (swingHighs.Count < 2 || swingLows.Count < 2)
                return;

            // 7. Estructura de mercado
            string structure;
            double lastHigh, lastLow;
            DetectMarketStructure(swingHighs, swingLows, idx,
                                  out structure, out lastHigh, out lastLow);
            if (structure == "RANGE")
                return;

            // 8. Ruptura fuerte
            var bar = Bars.Last(1);
            double body = Math.Abs(bar.Close - bar.Open);
            double wick = bar.High - bar.Low;
            double wickBodyRatio = body > 0 ? wick / body : 99;

            if (body < _currentBodyAvg * ForceBodyMultiplier)
                return;
            if (wickBodyRatio > MaxWickToBodyRatio)
                return;

            // 9. Ejecutar según estructura detectada
            if (structure == "BULLISH" && bar.High > lastHigh && bar.Close > lastHigh)
                TryPlaceBuyStop(idx, bar, structure, lastHigh);
            else if (structure == "BEARISH" && bar.Low < lastLow && bar.Close < lastLow)
                TryPlaceSellStop(idx, bar, structure, lastLow);
        }

        // ========================================================================
        // CÁLCULOS AUXILIARES
        // ========================================================================

        private void ComputeBodyAverage(int idx)
        {
            double sum = 0;
            int count = Math.Min(BodyAvgPeriod, idx);
            for (int i = idx - count; i < idx; i++)
                sum += Math.Abs(Bars[i].Close - Bars[i].Open);
            _currentBodyAvg = count > 0 ? sum / count : 0;
        }

        private void ResetDailyIfNeeded(int idx)
        {
            int currentDay = Bars.Last(1).OpenTime.DayOfYear;
            if (currentDay != _lastResetDay)
            {
                _tradesToday = 0;
                _tradesLondon = 0;
                _tradesNY = 0;
                _dailyStartEquity = Account.Equity;
                _dailyPnl = 0;
                _lastResetDay = currentDay;
                Print($"Reset diario. Equity={_dailyStartEquity:F2}");
            }
        }

        private bool CanTrade(int idx)
        {
            if (_tradesToday >= MaxTradesPerDay)
                return false;

            string session = GetSession();
            if (session == "NONE" && !CryptoMode)
                return false;
            if (session == "LONDON" && _tradesLondon >= MaxTradesPerSession)
                return false;
            if (session == "NY" && _tradesNY >= MaxTradesPerSession)
                return false;

            if (_dailyStartEquity > 0)
            {
                double lossPct = (_dailyPnl / _dailyStartEquity) * 100;
                if (lossPct <= -Math.Abs(MaxDailyLossPercent))
                {
                    Print($"Daily loss limit reached: {lossPct:F1}% (max {MaxDailyLossPercent}%)");
                    return false;
                }
            }
            return true;
        }

        private string GetSession()
        {
            if (CryptoMode) return "CRYPTO";
            var dt = Bars.Last(1).OpenTime;
            int mins = dt.Hour * 60 + dt.Minute;
            int avoid = AvoidFirstSessionMinutes;
            if (mins >= 420 + avoid && mins < 720) return "LONDON";
            if (mins >= 810 + avoid && mins < 1200) return "NY";
            return "NONE";
        }

        private void CleanExpiredOrders(int currentBar)
        {
            var toRemove = new List<string>();
            foreach (var kvp in _orderEntryBars)
            {
                if (currentBar - kvp.Value > PendingOrderExpiry)
                {
                    // Cancelar orden pendiente (si aún existe)
                    var pending = PendingOrders.FirstOrDefault(o => o.Label == kvp.Key);
                    if (pending != null)
                        CancelPendingOrder(pending);
                    toRemove.Add(kvp.Key);
                }
            }
            foreach (var key in toRemove)
                _orderEntryBars.Remove(key);
        }

        // ========================================================================
        // SWING HIGHS / LOWS
        // ========================================================================

        private List<int> FindSwingHighs(int idx)
        {
            var result = new List<int>();
            int start = Math.Max(SwingLookback, idx - StructureBars);
            int end = idx - SwingLookback - 1;
            for (int i = start; i <= end; i++)
            {
                bool isSwing = true;
                double h = Bars[i].High;
                for (int j = 1; j <= SwingLookback; j++)
                {
                    if (i - j < 0 || i + j >= idx)
                    { isSwing = false; break; }
                    if (h <= Bars[i - j].High || h <= Bars[i + j].High)
                    { isSwing = false; break; }
                }
                if (isSwing) result.Add(i);
            }
            return result;
        }

        private List<int> FindSwingLows(int idx)
        {
            var result = new List<int>();
            int start = Math.Max(SwingLookback, idx - StructureBars);
            int end = idx - SwingLookback - 1;
            for (int i = start; i <= end; i++)
            {
                bool isSwing = true;
                double l = Bars[i].Low;
                for (int j = 1; j <= SwingLookback; j++)
                {
                    if (i - j < 0 || i + j >= idx)
                    { isSwing = false; break; }
                    if (l >= Bars[i - j].Low || l >= Bars[i + j].Low)
                    { isSwing = false; break; }
                }
                if (isSwing) result.Add(i);
            }
            return result;
        }

        // ========================================================================
        // ESTRUCTURA DE MERCADO
        // ========================================================================

        private void DetectMarketStructure(List<int> swingHighs, List<int> swingLows, int idx,
            out string structure, out double lastHigh, out double lastLow)
        {
            int lastHIdx = swingHighs[swingHighs.Count - 1];
            int prevHIdx = swingHighs[swingHighs.Count - 2];
            int lastLIdx = swingLows[swingLows.Count - 1];
            int prevLIdx = swingLows[swingLows.Count - 2];

            double lh = Bars[lastHIdx].High;
            double ph = Bars[prevHIdx].High;
            double ll = Bars[lastLIdx].Low;
            double pl = Bars[prevLIdx].Low;

            if (lh > ph && ll > pl)
            {
                structure = "BULLISH";
                lastHigh = lh;
                lastLow = ll;
                return;
            }
            if (lh < ph && ll < pl)
            {
                structure = "BEARISH";
                lastHigh = lh;
                lastLow = ll;
                return;
            }
            structure = "RANGE";
            lastHigh = lh;
            lastLow = ll;
        }

        // ========================================================================
        // ZONA DE ENTRADA (soporta EntryMode 0 y 1)
        // ========================================================================

        private bool FindEntryZone(string structure, Bar breakoutBar, Bar prevBar,
            out double entryPrice, out double zoneEdge)
        {
            entryPrice = 0;
            zoneEdge = 0;

            if (structure == "BULLISH")
            {
                double zoneHigh = Math.Min(breakoutBar.Close, breakoutBar.High);
                double zoneLow = Math.Max(breakoutBar.Open, prevBar.Close);

                if (zoneHigh <= zoneLow) return false;

                if (EntryMode == 1) // conservative_edge: entrar cerca del borde superior
                    entryPrice = zoneHigh - (zoneHigh - zoneLow) * 0.2;
                else // midpoint (0)
                    entryPrice = zoneHigh - (zoneHigh - zoneLow) * 0.5;

                zoneEdge = zoneLow;
                return true;
            }
            else // BEARISH
            {
                double zoneLow = Math.Max(breakoutBar.Low, breakoutBar.Close);
                double zoneHigh = Math.Min(breakoutBar.Open, prevBar.Close);

                if (zoneHigh <= zoneLow) return false;

                if (EntryMode == 1) // conservative_edge: entrar cerca del borde inferior
                    entryPrice = zoneLow + (zoneHigh - zoneLow) * 0.2;
                else // midpoint (0)
                    entryPrice = zoneLow + (zoneHigh - zoneLow) * 0.5;

                zoneEdge = zoneHigh;
                return true;
            }
        }

        // ========================================================================
        // EJECUCIÓN DE ÓRDENES
        // ========================================================================

        private void TryPlaceBuyStop(int idx, Bar bar, string structure, double structuralHigh)
        {
            Bar prevBar = Bars[idx - 1];
            double entry, zoneEdge;
            if (!FindEntryZone("BULLISH", bar, prevBar, out entry, out zoneEdge))
                return;

            if (entry <= 0) return;

            double slPrice = structuralHigh - Symbol.PipSize * SlBufferPips;
            double riskPrice = Math.Abs(entry - slPrice);
            if (riskPrice <= 0) return;

            double tpPrice = entry + riskPrice * MinRR;
            double rr = (tpPrice - entry) / riskPrice;
            if (rr < MinRR) return;

            long volume = CalculateVolume(riskPrice);
            if (volume <= 0) return;

            double stopPips = (entry - Symbol.Bid) / Symbol.PipSize;
            double slPips = riskPrice / Symbol.PipSize;
            double tpPips = (tpPrice - entry) / Symbol.PipSize;

            string label = "FABIAN_BUY_" + idx;
            var result = PlaceStopOrder(TradeType.Buy, Symbol, volume, stopPips, label, slPips, tpPips);

            if (result.IsSuccessful)
            {
                _tradesToday++;
                _orderEntryBars[label] = idx;
                string s = GetSession();
                if (s == "LONDON") _tradesLondon++;
                if (s == "NY") _tradesNY++;
                Print($"BUY STOP | Entry={entry:F5} SL={slPrice:F5} TP={tpPrice:F5} RR={rr:F2} Vol={volume}");
            }
            else
                Print($"Error BuyStop: {result.Error}");
        }

        private void TryPlaceSellStop(int idx, Bar bar, string structure, double structuralLow)
        {
            Bar prevBar = Bars[idx - 1];
            double entry, zoneEdge;
            if (!FindEntryZone("BEARISH", bar, prevBar, out entry, out zoneEdge))
                return;

            if (entry <= 0) return;

            double slPrice = structuralLow + Symbol.PipSize * SlBufferPips;
            double riskPrice = Math.Abs(slPrice - entry);
            if (riskPrice <= 0) return;

            double tpPrice = entry - riskPrice * MinRR;
            double rr = (entry - tpPrice) / riskPrice;
            if (rr < MinRR) return;

            long volume = CalculateVolume(riskPrice);
            if (volume <= 0) return;

            double stopPips = (entry - Symbol.Ask) / Symbol.PipSize;
            double slPips = riskPrice / Symbol.PipSize;
            double tpPips = (entry - tpPrice) / Symbol.PipSize;

            string label = "FABIAN_SELL_" + idx;
            var result = PlaceStopOrder(TradeType.Sell, Symbol, volume, stopPips, label, slPips, tpPips);

            if (result.IsSuccessful)
            {
                _tradesToday++;
                _orderEntryBars[label] = idx;
                string s = GetSession();
                if (s == "LONDON") _tradesLondon++;
                if (s == "NY") _tradesNY++;
                Print($"SELL STOP | Entry={entry:F5} SL={slPrice:F5} TP={tpPrice:F5} RR={rr:F2} Vol={volume}");
            }
            else
                Print($"Error SellStop: {result.Error}");
        }

        private long CalculateVolume(double slPriceDistance)
        {
            if (UseRiskSizing && slPriceDistance > 0)
            {
                double riskAmount = Account.Equity * (RiskPercent / 100.0);
                double slInPips = slPriceDistance / Symbol.PipSize;
                double pipValue = Symbol.PipValue;
                double rawLots = riskAmount / (slInPips * pipValue);
                rawLots = Math.Round(rawLots, 2);
                if (rawLots < 0.01) rawLots = 0.01;
                return (long)Symbol.QuantityToVolumeInUnits(rawLots);
            }
            else
            {
                return (long)Symbol.QuantityToVolumeInUnits(VolumeLots);
            }
        }

        // ========================================================================
        // EVENTOS DE POSICIÓN
        // ========================================================================

#pragma warning disable CS0672
        protected override void OnPositionOpened(Position position)
        {
            if (position.Label.StartsWith("FABIAN"))
                Print($"Abierta: {position.TradeType} Vol={position.VolumeInUnits} @ {position.EntryPrice}");
        }

        protected override void OnPositionClosed(Position position)
        {
            if (!position.Label.StartsWith("FABIAN")) return;

            double pnl = position.GrossProfit;
            _dailyPnl += pnl;
            _peakBalance = Math.Max(_peakBalance, Account.Equity);

            if (pnl > 0)
            {
                _totalWins++;
                _consecutiveLosses = 0;
            }
            else if (pnl < 0)
            {
                _totalLosses++;
                _consecutiveLosses++;
            }
            _totalTrades++;

            // Hard kill: 3 pérdidas consecutivas → pausa 24 barras (~2h en 5m)
            if (_consecutiveLosses >= 3)
            {
                _pauseUntilBar = Bars.Count + 24;
                _consecutiveLosses = 0;
                Print("3 pérdidas consecutivas → pausa 2h");
            }

            Print($"Cerrada: {position.TradeType} PnL={pnl:F2} Bal={Account.Equity:F2}");
        }
#pragma warning restore CS0672

        // ========================================================================
        // TRAILING STOP + BREAK EVEN
        // ========================================================================

        protected override void OnTick()
        {
            if (!EnableTrailing) return;

            foreach (var pos in Positions.Where(p => p.Label.StartsWith("FABIAN")))
            {
                if (pos.TradeType == TradeType.Buy)
                {
                    double riskPips = Math.Abs(pos.EntryPrice - (pos.StopLoss ?? 0)) / Symbol.PipSize;
                    if (riskPips <= 0) continue;

                    // Break even at 1R — mover SL al entry una vez alcanzado 1R
                    if (pos.Pips >= riskPips && EnableBreakEvenAt1R && pos.StopLoss != pos.EntryPrice)
                    {
                        ModifyPosition(pos, pos.EntryPrice, pos.TakeProfit, ProtectionType.None);
                        Print($"BE {pos.Id} @ {pos.Pips:F1}pips");
                    }

                    // Trailing más agresivo: mover SL cada 0.5R adicional
                    if (pos.Pips >= riskPips * 1.5)
                    {
                        double newSl = pos.EntryPrice + riskPips * 0.5 * Symbol.PipSize;
                        if (newSl > (pos.StopLoss ?? 0))
                            ModifyPosition(pos, newSl, pos.TakeProfit, ProtectionType.None);
                    }
                }
                else if (pos.TradeType == TradeType.Sell)
                {
                    double riskPips = Math.Abs((pos.StopLoss ?? 0) - pos.EntryPrice) / Symbol.PipSize;
                    if (riskPips <= 0) continue;

                    if (pos.Pips >= riskPips && EnableBreakEvenAt1R && pos.StopLoss != pos.EntryPrice)
                    {
                        ModifyPosition(pos, pos.EntryPrice, pos.TakeProfit, ProtectionType.None);
                        Print($"BE {pos.Id} @ {pos.Pips:F1}pips");
                    }

                    if (pos.Pips >= riskPips * 1.5)
                    {
                        double newSl = pos.EntryPrice - riskPips * 0.5 * Symbol.PipSize;
                        if (newSl < (pos.StopLoss ?? double.MaxValue))
                            ModifyPosition(pos, newSl, pos.TakeProfit, ProtectionType.None);
                    }
                }
            }
        }

        // ========================================================================
        // ON STOP — resumen final
        // ========================================================================

        protected override void OnStop()
        {
            double winRate = _totalTrades > 0 ? (double)_totalWins / _totalTrades * 100 : 0;
            Print("=== RESUMEN FabianPullback v2 ===");
            Print($"Trades: {_totalTrades} | Wins: {_totalWins} | Losses: {_totalLosses}");
            Print($"WinRate: {winRate:F1}%");
            Print($"Balance: {Account.Equity:F2} | Peak: {_peakBalance:F2}");
            Print($"Drawdown: {(1 - Account.Equity / _peakBalance) * 100:F1}%");
        }
    }
}
