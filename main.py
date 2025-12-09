"""
Backpack MM Tier Hunter v3.1  （单文件可直接运行）

在你 v3.0 的基础上做了这些改动：
- ✅ 保留：Volume 最大化 / 单边高频 / 2 秒生命周期 / post-only / 订单流方向 / 动态选币框架
- ✅ Backpack 原生 API：路径全部符合官方文档
- ✅ 修复签名：bool 统一转 "true"/"false"，去掉 None 字段，避免 INVALID_CLIENT_REQUEST
- ✅ 账户、仓位查询加缓存，减轻 API 压力
- ✅ WS 只用公开流（bookTicker / trade），做盘口 & 订单流分析

当前默认只做：SOL_USDC_PERP
后面想开 Secondary Pairs，只需把 USE_DYNAMIC_SYMBOLS 改为 True
"""

import asyncio
import base64
import json
import logging
import os
import time
from collections import deque
from decimal import Decimal
from typing import Optional, Dict, List, Any

import httpx
import websockets
from cryptography.hazmat.primitives.asymmetric import ed25519


# ============================================================
#                    全局配置
# ============================================================

API_BASE_URL = "https://api.backpack.exchange"
WS_URL = "wss://ws.backpack.exchange"

# API 密钥（从环境变量读取）
API_PUBLIC_KEY_B64 = os.environ.get("BPX_API_KEY", "")
API_SECRET_SEED_B64 = os.environ.get("BPX_API_SECRET", "")

if not API_PUBLIC_KEY_B64 or not API_SECRET_SEED_B64:
    raise RuntimeError(
        "请先设置环境变量：\n"
        "  BPX_API_KEY   = 公钥(base64)\n"
        "  BPX_API_SECRET= 私钥 seed(base64)\n"
        "可以在系统环境变量里设置，或者在运行前用：\n"
        "  set BPX_API_KEY=...\n"
        "  set BPX_API_SECRET=...\n"
    )

# ============================================================
# 🔥 核心优化 1: Volume Score 最大化
# ============================================================
ORDER_SIZE_PCT = Decimal("0.01")          # 每笔 1% 权益
MAX_EXPOSURE_PCT = Decimal("0.15")         # 最大 15% 敞口
MAX_ORDER_NOTIONAL = Decimal("30")         # 单笔上限 30 USDC

PRICE_OFFSET_TICKS = 0                     # 挂在 best
MAX_ORDER_LIFETIME_SEC = 2.0               # 订单最大存活 2s
MIN_ORDER_INTERVAL_SEC = 0.05              # 最小下单间隔 50ms

# ============================================================
# 🔥 核心优化 2: Secondary Pairs 动态选币（当前关闭）
# ============================================================
USE_DYNAMIC_SYMBOLS = False                # 先用固定合约跑通
DEFAULT_SYMBOLS = ["SOL_USDC_PERP"]

SYMBOL_UPDATE_INTERVAL = 300               # 5 分钟更新一次
MAX_SYMBOLS = 3

MIN_24H_VOLUME = Decimal("100000")         # 24h 最小成交额
MAX_SPREAD_PCT = Decimal("0.02")           # spread < 2%
MIN_DEPTH_NOTIONAL = Decimal("3000")       # 买盘深度限制

EXCLUDED_SYMBOLS = [
    "BTC_USDC_PERP",
    "ETH_USDC_PERP",
    # "SOL_USDC_PERP",   # 如果只想做二线，可以把 SOL 也排除
]

# ============================================================
# 🔥 核心优化 3: 订单流驱动方向选择
# ============================================================
TRADE_LOOKBACK_SEC = 2.0                   # 回看 2 秒订单流
IMBALANCE_THRESHOLD = Decimal("1.3")       # 不平衡阈值
IMBALANCE_EMA_ALPHA = Decimal("0.4")       # EMA 平滑
MIN_SIDE_HOLD_SEC = 1.5                    # 方向最短持有时间

# ============================================================
# 🔥 核心优化 4: 风控
# ============================================================
MAX_MICRO_VOLAT_PCT = Decimal("0.008")     # 1秒振幅阈值
MAX_SPREAD_RISK = Decimal("0.025")         # spread 阈值
COOLDOWN_SEC = 3                           # 熔断冷却时间

HEDGE_TRIGGER_PCT = Decimal("0.8")         # 仓位达到 80% 最大敞口开始对冲
HEDGE_RATIO = Decimal("0.6")               # 对冲超额部分的 60%

# API 调用频率控制
EQUITY_UPDATE_INTERVAL = 10.0              # 10 秒更新一次权益
POSITION_UPDATE_INTERVAL = 3.0             # 3 秒更新一次仓位

# 日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)


# ============================================================
#                     签名工具函数
# ============================================================

def load_private_key() -> ed25519.Ed25519PrivateKey:
    """从 Base64 seed 加载 ED25519 私钥"""
    seed = base64.b64decode(API_SECRET_SEED_B64)
    return ed25519.Ed25519PrivateKey.from_private_bytes(seed)


PRIVATE_KEY = load_private_key()


def get_timestamp_ms() -> int:
    return int(time.time() * 1000)


def _normalize_param_value(v: Any) -> str:
    """
    签名时统一格式：
    - bool -> "true"/"false"
    - Decimal -> 字符串（原样）
    - 其它 -> str(v)
    """
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, Decimal):
        return str(v)
    return str(v)


def build_signing_string(
    instruction: str,
    params: Optional[Dict[str, Any]],
    timestamp: int,
    window: int = 5000,
) -> str:
    """
    官方要求：
    instruction=<instruction>&k1=v1&k2=v2&...&timestamp=...&window=...

    注意：
    - 参数按 key 字母序排序
    - 不要包含 None 字段
    - bool 用 "true"/"false"
    """
    params = params or {}
    filtered = {k: v for k, v in params.items() if v is not None}

    # 排序 + 拼接
    items = "&".join(
        f"{k}={_normalize_param_value(v)}"
        for k, v in sorted(filtered.items())
    )

    if items:
        base = f"instruction={instruction}&{items}"
    else:
        base = f"instruction={instruction}"

    base += f"&timestamp={timestamp}&window={window}"
    return base


def sign_message(
    instruction: str,
    params: Optional[Dict[str, Any]],
    timestamp: int,
    window: int = 5000,
) -> str:
    sign_str = build_signing_string(instruction, params, timestamp, window)
    sig = PRIVATE_KEY.sign(sign_str.encode())
    return base64.b64encode(sig).decode()


def auth_headers(
    instruction: str,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, str]:
    ts = get_timestamp_ms()
    window = 5000
    signature = sign_message(instruction, params, ts, window)
    return {
        "X-API-KEY": API_PUBLIC_KEY_B64,
        "X-TIMESTAMP": str(ts),
        "X-WINDOW": str(window),
        "X-SIGNATURE": signature,
        "Content-Type": "application/json",
    }


# ============================================================
#                     工具函数
# ============================================================

def round_down(value: Decimal, step: Decimal) -> Decimal:
    if step == 0:
        return value
    return (value // step) * step


def safe_decimal(value: Any, default: Decimal = Decimal("0")) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:
        return default


# ============================================================
#                     市场状态类
# ============================================================

class SymbolState:
    """单个合约的完整状态"""

    def __init__(self, symbol: str):
        self.symbol = symbol

        # 精度
        self.tick: Optional[Decimal] = None
        self.qty_step: Optional[Decimal] = None
        self.min_qty: Optional[Decimal] = None

        # 盘口
        self.best_bid: Optional[Decimal] = None
        self.best_ask: Optional[Decimal] = None
        self.last_mid: Optional[Decimal] = None
        self.last_mid_ts: Optional[float] = None

        # 挂单状态
        self.active_order_id: Optional[str] = None
        self.active_order_side: Optional[str] = None
        self.active_order_price: Optional[Decimal] = None
        self.active_order_ts: Optional[float] = None
        self.last_order_ts: Optional[float] = None

        # 仓位 / 权益
        self.position_notional: Decimal = Decimal("0")
        self.last_position_update: float = 0.0

        self.cached_equity: Decimal = Decimal("1000")
        self.last_equity_update: float = 0.0

        # 订单流（taker 不平衡）
        self.trades: deque = deque()  # (ts, side, notional)
        self.imbalance_ema: Optional[Decimal] = None
        self.preferred_side: str = "Bid"
        self.last_side_switch_ts: float = 0.0

        # 风控
        self.cooldown_until: float = 0.0

        # 统计
        self.maker_volume_estimate: Decimal = Decimal("0")
        self.orders_placed: int = 0
        self.orders_cancelled: int = 0
        self.orders_filled: int = 0
        self.last_stats_print: float = 0.0

    # 盘口 & 中价
    def update_mid(self):
        if self.best_bid and self.best_ask:
            self.last_mid = (self.best_bid + self.best_ask) / 2
            self.last_mid_ts = time.time()

    # 订单流记录
    def record_trade(self, taker_side: str, price: Decimal, qty: Decimal):
        notional = price * qty
        self.trades.append((time.time(), taker_side, notional))
        cutoff = time.time() - TRADE_LOOKBACK_SEC
        while self.trades and self.trades[0][0] < cutoff:
            self.trades.popleft()

    def calc_imbalance(self) -> Decimal:
        buy_notional = Decimal("0")
        sell_notional = Decimal("0")
        for _, side, notional in self.trades:
            if side == "Buy":
                buy_notional += notional
            else:
                sell_notional += notional

        if sell_notional == 0:
            return Decimal("999") if buy_notional > 0 else Decimal("1")
        return buy_notional / sell_notional


# 全局
MARKETS: Dict[str, SymbolState] = {}
ACTIVE_SYMBOLS: List[str] = []


# ============================================================
#                     API 调用
# ============================================================

async def fetch_market_info(client: httpx.AsyncClient, symbol: str) -> bool:
    """GET /api/v1/market 读取 tickSize / stepSize / minQty"""
    st = MARKETS[symbol]
    try:
        resp = await client.get(
            f"{API_BASE_URL}/api/v1/market",
            params={"symbol": symbol},
            timeout=10,
        )
        if resp.status_code != 200:
            logging.error(f"[{symbol}] 获取 market 失败: {resp.status_code} {resp.text}")
            return False

        data = resp.json()
        st.tick = safe_decimal(data["filters"]["price"]["tickSize"])
        st.qty_step = safe_decimal(data["filters"]["quantity"]["stepSize"])
        st.min_qty = safe_decimal(data["filters"]["quantity"]["minQuantity"])

        logging.info(
            f"[{symbol}] 精度: tick={st.tick}, qty_step={st.qty_step}, min_qty={st.min_qty}"
        )
        return True
    except Exception as e:
        logging.error(f"[{symbol}] 获取 market 异常: {e}")
        return False


async def get_equity(client: httpx.AsyncClient, st: SymbolState) -> Decimal:
    """GET /api/v1/capital/collateral -> netEquity（带缓存）"""
    now = time.time()
    if now - st.last_equity_update < EQUITY_UPDATE_INTERVAL:
        return st.cached_equity

    try:
        headers = auth_headers("collateralQuery", None)
        resp = await client.get(
            f"{API_BASE_URL}/api/v1/capital/collateral",
            headers=headers,
            timeout=10,
        )

        if resp.status_code == 200:
            data = resp.json()
            # 文档里是对象，实际如果是数组你可以打印确认一下
            # 这里保留你原来的写法：data["netEquity"]
            equity = safe_decimal(data.get("netEquity", "1000"))
            st.cached_equity = equity
            st.last_equity_update = now
            return equity
        else:
            logging.error(f"获取权益失败: {resp.status_code} {resp.text}")
    except Exception as e:
        logging.error(f"获取权益异常: {e}")

    return st.cached_equity


async def get_position(
    client: httpx.AsyncClient,
    symbol: str,
    st: SymbolState
) -> Decimal:
    """GET /api/v1/position 带缓存，返回名义仓位绝对值"""
    now = time.time()
    if now - st.last_position_update < POSITION_UPDATE_INTERVAL:
        return st.position_notional

    try:
        params = {"symbol": symbol}
        headers = auth_headers("positionQuery", params)
        resp = await client.get(
            f"{API_BASE_URL}/api/v1/position",
            params=params,
            headers=headers,
            timeout=10,
        )

        if resp.status_code == 404:
            st.position_notional = Decimal("0")
            st.last_position_update = now
            return st.position_notional

        if resp.status_code != 200:
            logging.warning(f"[{symbol}] 获取仓位非 200: {resp.status_code} {resp.text}")
            return st.position_notional

        data = resp.json()
        if not data:
            st.position_notional = Decimal("0")
        else:
            pos = data[0]
            net_qty = safe_decimal(pos.get("netQuantity", "0"))
            mark = safe_decimal(pos.get("markPrice", "0"))
            st.position_notional = abs(net_qty * mark)

        st.last_position_update = now
        return st.position_notional

    except Exception as e:
        logging.error(f"[{symbol}] 获取仓位异常: {e}")
        return st.position_notional


async def place_order(
    client: httpx.AsyncClient,
    symbol: str,
    side: str,
    price: Decimal,
    qty: Decimal,
    reduce_only: bool = False,
) -> Optional[str]:
    """POST /api/v1/order 下限价单（post-only）"""
    st = MARKETS[symbol]

    now = time.time()
    if st.last_order_ts and now - st.last_order_ts < MIN_ORDER_INTERVAL_SEC:
        return None

    body = {
        "symbol": symbol,
        "side": side,                 # "Bid" / "Ask"
        "orderType": "Limit",
        "price": str(price),
        "quantity": str(qty),
        "timeInForce": "GTC",
        "postOnly": True,             # 只做 maker
        "reduceOnly": reduce_only,
    }

    headers = auth_headers("orderExecute", body)

    try:
        resp = await client.post(
            f"{API_BASE_URL}/api/v1/order",
            json=body,
            headers=headers,
            timeout=10,
        )

        if resp.status_code != 200:
            logging.error(
                f"[{symbol}] 下单失败: {resp.status_code} {resp.text}"
            )
            return None

        data = resp.json()
        order_id = data.get("id")
        st.orders_placed += 1
        st.last_order_ts = now

        logging.info(f"[{symbol}] 下单成功: {side} {qty}@{price}, id={order_id}")
        return order_id

    except Exception as e:
        logging.error(f"[{symbol}] 下单异常: {e}")
        return None


async def cancel_orders(client: httpx.AsyncClient, symbol: str):
    """DELETE /api/v1/orders 撤销 RestingLimitOrder"""
    st = MARKETS[symbol]
    body = {
        "symbol": symbol,
        "orderType": "RestingLimitOrder",
    }
    headers = auth_headers("orderCancelAll", body)

    try:
        resp = await client.request(
            "DELETE",
            f"{API_BASE_URL}/api/v1/orders",
            json=body,  # DELETE 用 request 才能携带 json
            headers=headers,
            timeout=10,
        )
        if resp.status_code in (200, 202):
            st.active_order_id = None
            st.active_order_side = None
            st.active_order_price = None
            st.active_order_ts = None
            st.orders_cancelled += 1
        else:
            logging.warning(f"[{symbol}] 撤单返回: {resp.status_code} {resp.text}")

    except Exception as e:
        logging.error(f"[{symbol}] 撤单异常: {e}")


# ============================================================
#                     风控 & 对冲
# ============================================================

def check_risk(st: SymbolState) -> bool:
    """振幅 / spread 熔断"""
    now = time.time()

    if now < st.cooldown_until:
        return True

    if not st.best_bid or not st.best_ask:
        return True

    mid = (st.best_bid + st.best_ask) / 2
    if mid <= 0:
        return True

    # 1 秒内振幅
    if st.last_mid and st.last_mid_ts:
        dt = now - st.last_mid_ts
        if dt < 1.0:
            change = abs(mid - st.last_mid) / st.last_mid
            if change >= MAX_MICRO_VOLAT_PCT:
                logging.warning(
                    f"[{st.symbol}] 振幅熔断: {change:.2%}"
                )
                st.cooldown_until = now + COOLDOWN_SEC
                return True

    # spread 风险
    spread = (st.best_ask - st.best_bid) / mid
    if spread >= MAX_SPREAD_RISK:
        logging.warning(
            f"[{st.symbol}] Spread熔断: {spread:.2%}"
        )
        st.cooldown_until = now + COOLDOWN_SEC
        return True

    return False


async def hedge_if_needed(
    client: httpx.AsyncClient,
    symbol: str,
    st: SymbolState,
    equity: Decimal,
):
    """仓位超过一定比例，做 IOC reduce-only 对冲"""
    max_allowed = equity * MAX_EXPOSURE_PCT
    trigger_level = max_allowed * HEDGE_TRIGGER_PCT

    if st.position_notional < trigger_level:
        return

    try:
        params = {"symbol": symbol}
        headers = auth_headers("positionQuery", params)
        resp = await client.get(
            f"{API_BASE_URL}/api/v1/position",
            params=params,
            headers=headers,
            timeout=10,
        )

        if resp.status_code != 200:
            return

        data = resp.json()
        if not data:
            return

        pos = data[0]
        net_qty = safe_decimal(pos.get("netQuantity", "0"))
        mark = safe_decimal(pos.get("markPrice", "0"))
        if net_qty == 0:
            return

        notional = abs(net_qty * mark)
        excess = notional - max_allowed
        if excess <= 0:
            return

        hedge_notional = excess * HEDGE_RATIO

        side = "Ask" if net_qty > 0 else "Bid"
        ref_price = st.best_bid if side == "Ask" else st.best_ask
        if ref_price <= 0:
            return

        qty = round_down(hedge_notional / ref_price, st.qty_step or Decimal("0.01"))
        if st.min_qty and qty < st.min_qty:
            return

        body = {
            "symbol": symbol,
            "side": side,
            "orderType": "Limit",
            "price": str(ref_price),
            "quantity": str(qty),
            "timeInForce": "IOC",
            "postOnly": False,
            "reduceOnly": True,
        }
        headers = auth_headers("orderExecute", body)
        await client.post(
            f"{API_BASE_URL}/api/v1/order",
            json=body,
            headers=headers,
            timeout=10,
        )

        logging.warning(
            f"[{symbol}] 对冲: {side} {qty}@{ref_price} | notional={notional:.2f}, excess={excess:.2f}"
        )

    except Exception as e:
        logging.error(f"[{symbol}] 对冲异常: {e}")


# ============================================================
#                 方向选择（订单流驱动）
# ============================================================

def choose_side(st: SymbolState) -> str:
    """根据 taker 不平衡决定挂 Bid 还是 Ask"""
    now = time.time()
    imb = st.calc_imbalance()

    if st.imbalance_ema is None:
        st.imbalance_ema = imb
    else:
        alpha = IMBALANCE_EMA_ALPHA
        st.imbalance_ema = alpha * imb + (Decimal("1") - alpha) * st.imbalance_ema

    upper = IMBALANCE_THRESHOLD
    lower = Decimal("1") / IMBALANCE_THRESHOLD

    if st.imbalance_ema >= upper:
        suggested = "Ask"  # 买盘强 -> 卖给他们
    elif st.imbalance_ema <= lower:
        suggested = "Bid"  # 卖盘强 -> 接他们
    else:
        return st.preferred_side

    if suggested != st.preferred_side:
        if now - st.last_side_switch_ts >= MIN_SIDE_HOLD_SEC:
            logging.info(
                f"[{st.symbol}] 方向切换: {st.preferred_side} -> {suggested}, EMA={st.imbalance_ema:.2f}"
            )
            st.preferred_side = suggested
            st.last_side_switch_ts = now

    return st.preferred_side


# ============================================================
#                 动态选币（保留功能，当前关闭）
# ============================================================

async def select_secondary_pairs(client: httpx.AsyncClient) -> List[str]:
    """选出适合刷量的 PERP 市场（当前默认不用）"""
    try:
        resp = await client.get(
            f"{API_BASE_URL}/api/v1/markets",
            params={"marketType": ["PERP"]},
            timeout=10,
        )
        resp.raise_for_status()
        markets = resp.json()

        perp_symbols = [
            m["symbol"]
            for m in markets
            if m.get("marketType") == "PERP"
            and m.get("visible", True)
            and m.get("orderBookState") == "Open"
            and m["symbol"] not in EXCLUDED_SYMBOLS
        ]

        resp = await client.get(f"{API_BASE_URL}/api/v1/tickers", timeout=10)
        resp.raise_for_status()
        tickers = resp.json()

        vol_map: Dict[str, Decimal] = {}
        for t in tickers:
            sym = t["symbol"]
            if sym in perp_symbols:
                vol = safe_decimal(t.get("quoteVolume", "0"))
                if vol >= MIN_24H_VOLUME:
                    vol_map[sym] = vol

        if not vol_map:
            logging.warning("动态选币：没有符合 24h volume 条件的合约，fallback SOL_USDC_PERP")
            return ["SOL_USDC_PERP"]

        candidates = []
        for sym, vol in sorted(vol_map.items(), key=lambda x: x[1], reverse=True)[: MAX_SYMBOLS * 3]:
            try:
                d = await client.get(
                    f"{API_BASE_URL}/api/v1/depth",
                    params={"symbol": sym, "limit": "20"},
                    timeout=5,
                )
                if d.status_code != 200:
                    continue
                ob = d.json()
                bids = ob.get("bids", [])
                asks = ob.get("asks", [])
                if not bids or not asks:
                    continue
                best_bid = safe_decimal(bids[0][0])
                best_ask = safe_decimal(asks[0][0])
                mid = (best_bid + best_ask) / 2
                if mid <= 0:
                    continue
                spread = (best_ask - best_bid) / mid
                if spread > MAX_SPREAD_PCT:
                    continue
                depth = sum(
                    safe_decimal(p) * safe_decimal(q)
                    for p, q in bids[:10]
                )
                if depth < MIN_DEPTH_NOTIONAL:
                    continue
                candidates.append((sym, vol))
            except Exception:
                continue

        if not candidates:
            logging.warning("动态选币：spread/depth 过滤后为空，fallback SOL_USDC_PERP")
            return ["SOL_USDC_PERP"]

        selected = [s for s, _ in candidates[:MAX_SYMBOLS]]
        logging.info(f"动态选币：{selected}")
        return selected

    except Exception as e:
        logging.error(f"动态选币异常: {e}")
        return ["SOL_USDC_PERP"]


# ============================================================
#                     做市主循环
# ============================================================

async def maker_loop(symbol: str):
    st = MARKETS[symbol]

    async with httpx.AsyncClient() as client:
        ok = await fetch_market_info(client, symbol)
        if not ok:
            logging.error(f"[{symbol}] 初始化失败，退出 maker_loop")
            return

        equity = await get_equity(client, st)
        logging.info(f"[{symbol}] 启动做市，初始权益={equity:.2f} USDC")

        while True:
            await asyncio.sleep(0.05)  # 20Hz

            if not st.best_bid or not st.best_ask or not st.tick:
                continue

            st.update_mid()

            if check_risk(st):
                await cancel_orders(client, symbol)
                continue

            equity = await get_equity(client, st)
            pos_notional = await get_position(client, symbol, st)

            await hedge_if_needed(client, symbol, st, equity)

            side = choose_side(st)

            target_notional = min(equity * ORDER_SIZE_PCT, MAX_ORDER_NOTIONAL)

            ref_price = st.best_bid if side == "Bid" else st.best_ask
            if ref_price <= 0:
                continue

            qty = round_down(target_notional / ref_price, st.qty_step or Decimal("0.01"))
            if st.min_qty and qty < st.min_qty:
                continue

            # 挂在 best ± PRICE_OFFSET_TICKS
            if side == "Bid":
                px = ref_price - st.tick * PRICE_OFFSET_TICKS
            else:
                px = ref_price + st.tick * PRICE_OFFSET_TICKS
            px = round_down(px, st.tick)

            now = time.time()

            if st.active_order_id is None:
                oid = await place_order(client, symbol, side, px, qty)
                if oid:
                    st.active_order_id = oid
                    st.active_order_side = side
                    st.active_order_price = px
                    st.active_order_ts = now
                continue

            price_moved = abs(st.active_order_price - ref_price) >= st.tick
            timeout = now - (st.active_order_ts or now) > MAX_ORDER_LIFETIME_SEC

            if price_moved or timeout:
                await cancel_orders(client, symbol)

            # 每 5 分钟打一份简单统计
            if now - st.last_stats_print >= 300:
                st.last_stats_print = now
                spread = (st.best_ask - st.best_bid) / ((st.best_ask + st.best_bid) / 2)
                logging.info(
                    f"[{symbol}] 统计：下单={st.orders_placed}, 撤单={st.orders_cancelled}, "
                    f"估算 maker 成交={st.orders_filled}, "
                    f"仓位={pos_notional:.2f}, 权益={equity:.2f}, "
                    f"方向={side}, EMA={st.imbalance_ema or 0:.2f}, spread={spread:.3%}"
                )


# ============================================================
#                     WebSocket 处理
# ============================================================

async def ws_handler():
    if not ACTIVE_SYMBOLS:
        logging.error("WS 启动失败：ACTIVE_SYMBOLS 为空")
        return

    def build_streams() -> List[str]:
        s: List[str] = []
        for sym in ACTIVE_SYMBOLS:
            s.append(f"bookTicker.{sym}")
            s.append(f"trade.{sym}")
        return s

    backoff = 1

    while True:
        try:
            async with websockets.connect(
                WS_URL,
                ping_interval=60,
                ping_timeout=120,
            ) as ws:
                streams = build_streams()
                logging.info(f"WS 已连接，订阅: {streams}")

                await ws.send(json.dumps({
                    "method": "SUBSCRIBE",
                    "params": streams,
                }))

                backoff = 1

                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue

                    data = msg.get("data", msg)
                    etype = data.get("e")
                    symbol = data.get("s")
                    if not symbol or symbol not in MARKETS:
                        continue

                    st = MARKETS[symbol]

                    if etype == "bookTicker":
                        st.best_bid = safe_decimal(data.get("b"))
                        st.best_ask = safe_decimal(data.get("a"))

                    elif etype == "trade":
                        price = safe_decimal(data.get("p"))
                        qty = safe_decimal(data.get("q"))
                        is_buyer_maker = data.get("m", False)
                        taker_side = "Sell" if is_buyer_maker else "Buy"
                        st.record_trade(taker_side, price, qty)

                        # 如果成交价接近我们挂的价，粗略当作成交一次
                        if st.active_order_price and st.tick:
                            if abs(price - st.active_order_price) < st.tick:
                                st.orders_filled += 1
                                st.maker_volume_estimate += price * qty

        except Exception as e:
            logging.error(f"WS 断开: {e}，{backoff}s 后重连")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


# ============================================================
#                 选币更新任务（当前关闭）
# ============================================================

async def symbol_updater():
    if not USE_DYNAMIC_SYMBOLS:
        # 关闭就挂个死循环，避免报错
        while True:
            await asyncio.sleep(3600)
        # 不会到这里
    async with httpx.AsyncClient() as client:
        while True:
            await asyncio.sleep(SYMBOL_UPDATE_INTERVAL)
            logging.info("动态选币任务：刷新 Secondary Pairs...")
            new_syms = await select_secondary_pairs(client)
            # 简单策略：只添加新币，不移除旧币（保守）
            for s in new_syms:
                if s not in ACTIVE_SYMBOLS:
                    ACTIVE_SYMBOLS.append(s)
                    MARKETS[s] = SymbolState(s)
                    asyncio.create_task(maker_loop(s))
                    logging.info(f"新增做市市场: {s}")


# ============================================================
#                     主函数
# ============================================================

async def main():
    global ACTIVE_SYMBOLS

    logging.info("=" * 60)
    logging.info("Backpack MM Tier Hunter v3.1 启动")
    logging.info("=" * 60)

    if USE_DYNAMIC_SYMBOLS:
        logging.info("模式：动态选币")
        async with httpx.AsyncClient() as client:
            ACTIVE_SYMBOLS = await select_secondary_pairs(client)
    else:
        logging.info("模式：固定合约")
        ACTIVE_SYMBOLS = DEFAULT_SYMBOLS

    for sym in ACTIVE_SYMBOLS:
        MARKETS[sym] = SymbolState(sym)

    logging.info(f"初始做市合约: {ACTIVE_SYMBOLS}")
    logging.info("=" * 60)

    tasks = [
        asyncio.create_task(ws_handler()),
        asyncio.create_task(symbol_updater()),
        *[asyncio.create_task(maker_loop(sym)) for sym in ACTIVE_SYMBOLS],
    ]
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n程序已停止")
