# Backpack High-Frequency Market Making Bot

A fully engineered high-frequency **post-only market making bot** for  
**Backpack Perpetual Futures (PERP)**.  

This system is designed to:
- Generate **high maker volume** (to reach higher MM tiers)
- Maintain **near-zero directional exposure**
- React quickly to microstructural changes
- Minimize adverse selection
- Stay online reliably with automatic WS reconnection
- Hedge excess inventory with minimal slippage

All components are implemented andproduction-ready.  
API signing (ED25519 + Base64), REST routing, WebSocket parsing, and risk modules are all functional.

---

#  Features

## 1. **Direction-Aware One-Sided Market Making**
The bot *never* posts both sides simultaneously.  
Instead, it **dynamically switches between Bid and Ask** based on short-term taker order flow.

### Orderflow-based side selection:
- If taker *Buy* notional dominates → post **Ask**
- If taker *Sell* notional dominates → post **Bid**
- Neutral regime → keep current side (with minimum hold time)

This approach achieves:
- Higher maker fills
- Lower inventory risk  
- Better protection against momentum-driven adverse selection

Direction detection uses:
- A rolling window of recent trades (3 seconds)
- Buy/Sell notional imbalance
- EMA smoothing
- Minimum side-hold period to prevent oscillation

---

## 2. **Post-Only Maker Engine (High-Frequency Cancel/Replace)**
The bot runs an aggressive “tick-level” quoting loop:

- Requotes every 100ms
- Cancels and re-posts if:
  - Quoted price deviates ≥ 1 tick from best price
  - Quotation lifetime exceeds 3 seconds
  - Risk trigger fires

All orders are **postOnly=True** maker orders  
→ Not subject to Backpack’s 100ms speed bump.

This allows safe, efficient, high-frequency maker-side participation.

---

## 3. **Dynamic Order Sizing Based on Net Equity**
Order size is proportional to account equity:

```

order_notional = equity * ORDER_SIZE_PCT

```

and capped:

```

MAX_ORDER_NOTIONAL = 25 USDC

```

This sizing method provides:
- Automatic scaling when equity grows (compounding)
- Automatic de-risking after drawdowns
- Stable behavior across different account sizes

---

## 4. **Comprehensive Multi-Layer Risk Management**

###  4.1 Micro-Volatility Circuit Breaker  
If 1-second mid-price movement exceeds `0.6%`:
- Cancel all quotes  
- Enter a 5-second cooldown

###  4.2 Spread Risk  
If spread exceeds `2%`:
- Cancel all orders  
- Enter cooldown

###  4.3 Inventory Notional Cap  
Actual inventory notional:
```

|netQty * markPrice|

```
If it exceeds:
```

equity * MAX_EXPOSURE_PCT

```
→ Bot stops quoting and triggers inventory hedge.

This ensures directional exposure stays controlled at all times.

---

## 5. **Inventory Hedge via Limit IOC (Reduce-Only)**
When excess exposure is detected:

- Long exposure → place **Ask IOC reduce-only** near bestBid  
- Short exposure → place **Bid IOC reduce-only** near bestAsk  

Key benefits:
- Avoids using market orders  
- Minimizes slippage  
- Uses top-of-book liquidity  
- Compatible with Backpack’s post-only speedbump rules  
- Ensures hedge orders never build a new position

This hedging logic is **fully implemented** and tested against API limits.

---

## 6. **WebSocket Auto-Reconnect (Exponential Backoff)**
The bot listens to:

- `bookTicker.<symbol>`
- `trade.<symbol>`

The WS layer includes:
- Automatic reconnection  
- Re-subscription  
- Exponential backoff  
- Heartbeat/ping configuration  
- Graceful recovery without leaving stale orders behind  

This allows uninterrupted multi-day operation.

---

## 7. **Dynamic Market Selection Framework (Currently Disabled)**
The code includes a complete framework for selecting the best PERP markets using:

- 24h quoteVolume  
- Spread  
- Top-of-book depth  
- Market status (visible / open)

For now, dynamic selection is disabled on purpose (single-market testing),  
but the full logic is implemented and ready to be enabled.

---

#  Architecture Overview

```

WebSocket Streams (bookTicker + trade)
↓
Short-Term Orderflow Analyzer
↓
Side Selector (Bid / Ask)
↓
Maker Engine (post-only limit quoting)
↓
Risk Manager
(micro-vol, spread, inventory)
↓
Inventory Hedge (reduce-only IOC)

````




#  Notes & Disclaimer

* This is a **high-frequency maker strategy**, not a directional strategy.
* While risk is minimized, it cannot eliminate:

  * Structural liquidity gaps
  * Deep book sweeps
  * Large taker imbalances during WS delays
  * Speedbump delays on reduce-only hedges

Always validate with small size first.

---


