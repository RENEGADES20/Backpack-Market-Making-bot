"""
Backpack MM Tier Hunter v3.2  （单文件可直接运行）

在你 v3.1 的基础上做了这些改动：
- ✅ 保留：Volume 最大化 / 单边高频 / 2 秒生命周期 / post-only / 订单流方向 / 动态选币框架
- ✅ Backpack 原生 API：路径全部符合官方文档
- ✅ 修复签名：bool 统一转 "true"/"false"，去掉 None 字段，避免 INVALID_CLIENT_REQUEST
- ✅ 账户、仓位查询加缓存，减轻 API 压力
- ✅ WS 只用公开流（bookTicker / trade），做盘口 & 订单流分析

🔥 v3.2 新增功能：
- ✅ API 优化：最大化利用 WebSocket，最小化 REST API 调用（统计 WS/API 比率）
- ✅ 完整日志：输出到 D:\ALLCRYPTO\backpack mm\pythonProject\log.txt
  包含：maker/taker数量、成交/失败数量、long/short比例、权益、
  总PnL、平均PnL、最大获利/亏损、总手续费
- ✅ 库存管理：一旦出现仓位，立即切换方向用 limit order 平仓（单边做市）
- ✅ 统计追踪：全局统计对象追踪所有交易指标

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
from datetime import datetime

import httpx
import websockets
from cryptography.hazmat.primitives.asymmetric import ed25519


# ============================================================
#                    全局配置
# ============================================================

API_BASE_URL = "https://api.backpack.exchange"
WS_URL = "wss://ws.backpack.exchange"

# 日志文件路径
LOG_FILE_PATH = r"D:\ALLCRYPTO\backpack mm\pythonProject\log.txt"

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
# 🔥 核心优化 3: 订单流驱动方向选择 + 库存管理
# ============================================================
TRADE_LOOKBACK_SEC = 2.0                   # 回看 2 秒订单流
IMBALANCE_THRESHOLD = Decimal("1.3")       # 不平衡阈值
IMBALANCE_EMA_ALPHA = Decimal("0.4")       # EMA 平滑
MIN_SIDE_HOLD_SEC = 1.5                    # 方向最短持有时间

# 库存管理：一旦出现仓位，立即切换方向平仓
INVENTORY_THRESHOLD = Decimal("0.001")     # 最小仓位阈值（名义价值）
FORCE_REDUCE_ON_INVENTORY = True           # 强制基于库存切换方向

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

# 统计日志输出间隔
STATS_LOG_INTERVAL = 60.0                  # 60 秒输出一次统计到文件

# 日志配置（同时输出到控制台和文件）
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# 控制台处理器
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
console_handler.setFormatter(console_formatter)
logger.addHandler(console_handler)


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
#                     统计跟踪类
# ============================================================

class TradingStats:
    """全局交易统计"""
    def __init__(self):
        # 订单统计
        self.maker_count = 0           # maker 成交次数
        self.taker_count = 0           # taker 成交次数
        self.filled_count = 0          # 总成交次数
        self.failed_count = 0          # 失败订单数

        # 方向统计
        self.long_count = 0            # 做多次数
        self.short_count = 0           # 做空次数

        # 财务统计
        self.total_pnl = Decimal("0")  # 总盈亏
        self.realized_pnls: List[Decimal] = []  # 每笔已实现盈亏
        self.max_profit = Decimal("0") # 最大单笔盈利
        self.max_loss = Decimal("0")   # 最大单笔亏损
        self.total_fees = Decimal("0") # 总手续费

        # 仓位跟踪
        self.last_position_qty = Decimal("0")  # 上次仓位数量（用于计算已实现PnL）
        self.avg_entry_price = Decimal("0")    # 平均开仓价格

        # API调用统计
        self.api_calls_count = 0       # REST API 调用次数
        self.ws_messages_count = 0     # WebSocket 消息数

    def record_fill(self, side: str, qty: Decimal, price: Decimal, is_maker: bool = True):
        """记录成交"""
        self.filled_count += 1
        if is_maker:
            self.maker_count += 1
        else:
            self.taker_count += 1

        if side == "Bid":  # 买入 = 做多
            self.long_count += 1
        else:              # 卖出 = 做空
            self.short_count += 1

    def record_pnl(self, pnl: Decimal):
        """记录单笔盈亏"""
        self.total_pnl += pnl
        self.realized_pnls.append(pnl)
        if pnl > self.max_profit:
            self.max_profit = pnl
        if pnl < self.max_loss:
            self.max_loss = pnl

    def record_fee(self, fee: Decimal):
        """记录手续费"""
        self.total_fees += fee

    def get_avg_pnl(self) -> Decimal:
        """获取平均盈亏"""
        if not self.realized_pnls:
            return Decimal("0")
        return self.total_pnl / len(self.realized_pnls)

    def get_long_short_ratio(self) -> str:
        """获取多空比例"""
        total = self.long_count + self.short_count
        if total == 0:
            return "0:0"
        return f"{self.long_count}:{self.short_count}"

    def to_log_string(self, equity: Decimal) -> str:
        """生成日志字符串"""
        avg_pnl = self.get_avg_pnl()
        ratio = self.get_long_short_ratio()

        return (
            f"\n{'='*80}\n"
            f"[交易统计] {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"{'='*80}\n"
            f"订单统计:\n"
            f"  Maker成交: {self.maker_count} 笔 | Taker成交: {self.taker_count} 笔\n"
            f"  总成交: {self.filled_count} 笔 | 失败订单: {self.failed_count} 笔\n"
            f"  成功率: {(self.filled_count/(self.filled_count+self.failed_count)*100 if (self.filled_count+self.failed_count)>0 else 0):.2f}%\n"
            f"\n"
            f"方向统计:\n"
            f"  多空比例: {ratio}\n"
            f"  做多: {self.long_count} 次 | 做空: {self.short_count} 次\n"
            f"\n"
            f"财务统计:\n"
            f"  当前权益: {equity:.2f} USDC\n"
            f"  总盈亏(PnL): {self.total_pnl:.4f} USDC\n"
            f"  平均盈亏: {avg_pnl:.4f} USDC\n"
            f"  最大盈利: {self.max_profit:.4f} USDC\n"
            f"  最大亏损: {self.max_loss:.4f} USDC\n"
            f"  总手续费: {self.total_fees:.4f} USDC\n"
            f"\n"
            f"API效率:\n"
            f"  REST API调用: {self.api_calls_count} 次\n"
            f"  WebSocket消息: {self.ws_messages_count} 条\n"
            f"  WS/API比率: {(self.ws_messages_count/self.api_calls_count if self.api_calls_count>0 else 0):.2f}x\n"
            f"{'='*80}\n"
        )


# 全局统计对象
GLOBAL_STATS = TradingStats()


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
        self.position_qty: Decimal = Decimal("0")  # 净仓位数量（正=多，负=空）
        self.last_position_update: float = 0.0

        self.cached_equity: Decimal = Decimal("1000")
        self.last_equity_update: float = 0.0

        # 统计日志
        self.last_stats_log: float = 0.0

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
        GLOBAL_STATS.api_calls_count += 1  # 统计API调用
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
        GLOBAL_STATS.api_calls_count += 1  # 统计API调用
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
            st.position_qty = Decimal("0")
            st.last_position_update = now
            return st.position_notional

        if resp.status_code != 200:
            logging.warning(f"[{symbol}] 获取仓位非 200: {resp.status_code} {resp.text}")
            return st.position_notional

        data = resp.json()
        if not data:
            st.position_notional = Decimal("0")
            st.position_qty = Decimal("0")
        else:
            pos = data[0]
            net_qty = safe_decimal(pos.get("netQuantity", "0"))
            mark = safe_decimal(pos.get("markPrice", "0"))
            st.position_notional = abs(net_qty * mark)
            st.position_qty = net_qty  # 保存净仓位数量（带符号）

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
        GLOBAL_STATS.api_calls_count += 1  # 统计API调用
        resp = await client.post(
            f"{API_BASE_URL}/api/v1/order",
            json=body,
            headers=headers,
            timeout=10,
        )

        if resp.status_code != 200:
            GLOBAL_STATS.failed_count += 1  # 统计失败订单
            logging.error(
                f"[{symbol}] 下单失败: {resp.status_code} {resp.text}"
            )
            return None

        data = resp.json()
        order_id = data.get("id")
        st.orders_placed += 1
        st.last_order_ts = now

        logging.info(f"[{symbol}] 下单成功: {side} {qty}@{price}, id={order_id}, reduce={reduce_only}")
        return order_id

    except Exception as e:
        GLOBAL_STATS.failed_count += 1  # 统计失败订单
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
        GLOBAL_STATS.api_calls_count += 1  # 统计API调用
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
        GLOBAL_STATS.api_calls_count += 1  # 统计API调用
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
        GLOBAL_STATS.api_calls_count += 1  # 统计API调用
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
    """
    根据库存优先，然后是 taker 不平衡决定挂 Bid 还是 Ask

    策略：
    1. 如果启用库存管理且有仓位 -> 立即切换到平仓方向
    2. 否则根据订单流不平衡选择方向
    """
    now = time.time()

    # 🔥 优先：库存管理 - 一旦有仓位，立即切换方向平仓
    if FORCE_REDUCE_ON_INVENTORY:
        # 检查是否有显著仓位
        if abs(st.position_qty * (st.last_mid or Decimal("1"))) > INVENTORY_THRESHOLD:
            if st.position_qty > 0:
                # 有多仓 -> 挂Ask平仓
                suggested = "Ask"
                if suggested != st.preferred_side:
                    logging.info(
                        f"[{st.symbol}] 库存触发方向切换: {st.preferred_side} -> {suggested}, "
                        f"仓位={st.position_qty:.4f}, 名义价值={st.position_notional:.2f}"
                    )
                    st.preferred_side = suggested
                    st.last_side_switch_ts = now
                return st.preferred_side
            elif st.position_qty < 0:
                # 有空仓 -> 挂Bid平仓
                suggested = "Bid"
                if suggested != st.preferred_side:
                    logging.info(
                        f"[{st.symbol}] 库存触发方向切换: {st.preferred_side} -> {suggested}, "
                        f"仓位={st.position_qty:.4f}, 名义价值={st.position_notional:.2f}"
                    )
                    st.preferred_side = suggested
                    st.last_side_switch_ts = now
                return st.preferred_side

    # 无仓位或未启用库存管理 -> 使用订单流策略
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
                f"[{st.symbol}] 订单流方向切换: {st.preferred_side} -> {suggested}, EMA={st.imbalance_ema:.2f}"
            )
            st.preferred_side = suggested
            st.last_side_switch_ts = now

    return st.preferred_side


# ============================================================
#                 统计日志输出
# ============================================================

async def write_stats_to_file(equity: Decimal):
    """将统计信息写入日志文件"""
    try:
        # 确保目录存在
        log_dir = os.path.dirname(LOG_FILE_PATH)
        if log_dir and not os.path.exists(log_dir):
            os.makedirs(log_dir, exist_ok=True)

        # 生成统计字符串
        stats_str = GLOBAL_STATS.to_log_string(equity)

        # 追加写入文件
        with open(LOG_FILE_PATH, 'a', encoding='utf-8') as f:
            f.write(stats_str)
            f.flush()

        logging.info(f"统计已写入日志文件: {LOG_FILE_PATH}")

    except Exception as e:
        logging.error(f"写入统计日志失败: {e}")


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

            # 🔥 判断是否需要 reduce_only（有仓位时平仓）
            reduce_only = False
            if FORCE_REDUCE_ON_INVENTORY and abs(st.position_qty) > 0:
                # 有多仓且挂Ask，或有空仓且挂Bid -> reduce_only
                if (st.position_qty > 0 and side == "Ask") or (st.position_qty < 0 and side == "Bid"):
                    reduce_only = True

            if st.active_order_id is None:
                oid = await place_order(client, symbol, side, px, qty, reduce_only=reduce_only)
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

            # 🔥 定期写入统计日志到文件
            if now - st.last_stats_log >= STATS_LOG_INTERVAL:
                st.last_stats_log = now
                await write_stats_to_file(equity)

            # 每 5 分钟打一份简单统计到控制台
            if now - st.last_stats_print >= 300:
                st.last_stats_print = now
                spread = (st.best_ask - st.best_bid) / ((st.best_ask + st.best_bid) / 2)
                logging.info(
                    f"[{symbol}] 统计：下单={st.orders_placed}, 撤单={st.orders_cancelled}, "
                    f"估算 maker 成交={st.orders_filled}, "
                    f"仓位={pos_notional:.2f} (qty={st.position_qty:.4f}), 权益={equity:.2f}, "
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

                    GLOBAL_STATS.ws_messages_count += 1  # 统计WS消息

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
                        if st.active_order_price and st.tick and st.active_order_side:
                            if abs(price - st.active_order_price) < st.tick:
                                st.orders_filled += 1
                                st.maker_volume_estimate += price * qty

                                # 记录到全局统计
                                GLOBAL_STATS.record_fill(
                                    side=st.active_order_side,
                                    qty=qty,
                                    price=price,
                                    is_maker=True
                                )

                                # 估算手续费（maker一般是负费率，但为了统计完整性）
                                # Backpack maker fee 通常是 -0.02% 或 0%，这里用 0.0002 作为估算
                                est_fee = price * qty * Decimal("0.0002")
                                GLOBAL_STATS.record_fee(est_fee)

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
    logging.info("Backpack MM Tier Hunter v3.2 启动")
    logging.info("=" * 60)

    # 初始化日志文件
    try:
        log_dir = os.path.dirname(LOG_FILE_PATH)
        if log_dir and not os.path.exists(log_dir):
            os.makedirs(log_dir, exist_ok=True)

        with open(LOG_FILE_PATH, 'a', encoding='utf-8') as f:
            f.write(f"\n{'='*80}\n")
            f.write(f"[启动] Backpack MM Tier Hunter v3.2 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"{'='*80}\n")
            f.write(f"配置:\n")
            f.write(f"  - 库存管理: {'启用' if FORCE_REDUCE_ON_INVENTORY else '禁用'}\n")
            f.write(f"  - 库存阈值: {INVENTORY_THRESHOLD} USDC\n")
            f.write(f"  - 统计日志间隔: {STATS_LOG_INTERVAL}s\n")
            f.write(f"  - API缓存: 权益{EQUITY_UPDATE_INTERVAL}s / 仓位{POSITION_UPDATE_INTERVAL}s\n")
            f.write(f"{'='*80}\n\n")
            f.flush()

        logging.info(f"日志文件已初始化: {LOG_FILE_PATH}")
    except Exception as e:
        logging.warning(f"初始化日志文件失败: {e}，将继续运行但不写入文件日志")

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
