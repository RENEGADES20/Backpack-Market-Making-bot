"""
Backpack 高频做市机器人 v2.0（单文件版）

核心功能：
1）动态选择二线永续合约（按 24h 成交量 / 深度 / spread）
2）单边做市 + 动态切换 Bid / Ask 方向（基于 taker 订单流不平衡）
3）下单规模 = 账户净权益百分比（支持滚仓复利）
4）多维风控：短期振幅 + spread 熔断 + 仓位上限
5）超仓位时，使用“限价 IOC 反向平仓”对冲超额部分，而不是粗暴市价
6）WebSocket 自动重连，恢复订阅，稳定更新盘口和成交
7）大量中文注释，适合你后续继续魔改



"""

import asyncio
import base64
import json
import logging
import os
import time
from collections import deque
from decimal import Decimal

import httpx
import websockets
from cryptography.hazmat.primitives.asymmetric import ed25519


# ============================================================
#                      全局配置（需按需修改）
# ============================================================

API_BASE_URL = "https://api.backpack.exchange"
WS_URL = "wss://ws.backpack.exchange"

# 这里按官方文档：X-API-KEY = base64 公钥，X-SIGNATURE 用 seed(私钥) 签
API_PUBLIC_KEY_B64 = os.environ.get("BPX_API_KEY", "")
API_SECRET_SEED_B64 = os.environ.get("BPX_API_SECRET", "")

if not API_PUBLIC_KEY_B64 or not API_SECRET_SEED_B64:
    raise RuntimeError("请先设置环境变量 BPX_API_KEY（公钥）和 BPX_API_SECRET（seed 私钥，base64）")

# ---------- 仓位与下单比例 ----------
ORDER_SIZE_PCT = Decimal("0.005")     # 每笔下单 = 账户权益 * 0.5%
MAX_EXPOSURE_PCT = Decimal("0.20")    # 总敞口 = 账户权益 * 20%

MAX_ORDER_NOTIONAL = Decimal("25")    # 单笔名义最多 25U

MAX_ACTIVE_LIFETIME_SEC = 3.0         # 单笔挂单最多挂 3 秒
PRICE_OFFSET_TICKS = 1                # 0=挂在 best；1=best±1tick

# ---------- 动态选币 ----------
MIN_24H_VOL = Decimal("200000")       # 24h quoteVolume 下限（按 USDC 计）
MAX_SPREAD_PCT = Decimal("0.015")     # spread < 1.5% 才做
MIN_DEPTH = Decimal("5000")           # 买盘深度 > 5000U 才做（粗略）
MAX_SYMBOLS = 5                       # 同时做市的合约数量
DEPTH_LEVELS_FOR_FILTER = 10          # 用前多少档粗略估算深度

# ---------- 多维风控 ----------
MAX_SHORT_VOLAT_PCT = Decimal("0.006")    # 1秒内 mid 振幅 > 0.6% → 熔断
MAX_SPREAD_RISK = Decimal("0.02")         # spread > 2% → 熔断
COOLDOWN_SEC = 5                          # 熔断后冷静期

# ---------- taker-speed / 方向判断 ----------
TRADE_LOOKBACK_SEC = 3.0                  # 回看过去 3 秒买卖不平衡
IMBALANCE_THRESHOLD = Decimal("1.2")      # 买卖不平衡比例 1.2 倍才算有效信号

# 方向平滑相关
IMBALANCE_EMA_ALPHA = Decimal("0.3")      # EMA 平滑系数（越大越敏感）
MIN_SIDE_HOLD_SEC = 2.0                   # 方向切换最小间隔（秒）

# ---------- 日志 ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)


# ============================================================
#                     签名相关工具函数（REST）
# ============================================================

def load_private_key():
    """从 Base64 的 Seed 恢复 ED25519 私钥（官方文档同款生成方式）。"""
    seed = base64.b64decode(API_SECRET_SEED_B64)
    return ed25519.Ed25519PrivateKey.from_private_bytes(seed)


PRIVATE_KEY = load_private_key()


def get_timestamp_ms() -> int:
    return int(time.time() * 1000)


def build_signing_string(instruction: str, params: dict | None, timestamp: int, window: int) -> str:
    """
    Backpack 官方签名格式：

    1）把 body 或 query 的 key/value 按字母序排序，拼成 query-string
    2）前面加上 instruction=...
    3）后面追加 &timestamp=...&window=...

    instruction=<instruction>&k1=v1&k2=v2&timestamp=...&window=...
    """
    params = params or {}
    items = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    if items:
        base = f"instruction={instruction}&{items}"
    else:
        base = f"instruction={instruction}"
    base += f"&timestamp={timestamp}&window={window}"
    return base


def sign(instruction: str, params: dict | None, timestamp: int, window: int) -> str:
    msg = build_signing_string(instruction, params, timestamp, window).encode()
    sig = PRIVATE_KEY.sign(msg)
    return base64.b64encode(sig).decode()


def auth_headers(instruction: str, params: dict | None = None) -> dict:
    ts = get_timestamp_ms()
    window = 5000
    signature = sign(instruction, params, ts, window)
    return {
        "X-API-KEY": API_PUBLIC_KEY_B64,
        "X-TIMESTAMP": str(ts),
        "X-WINDOW": str(window),
        "X-SIGNATURE": signature,
        "Content-Type": "application/json",
    }


# ============================================================
#                 公共工具：精度处理 / 取整
# ============================================================

def round_down_to_step(value: Decimal, step: Decimal) -> Decimal:
    """向下取整到 step 的整数倍。如 value=1.234, step=0.01 -> 1.23"""
    if step == 0:
        return value
    return (value // step) * step


# ============================================================
#                      符号级（单合约）状态
# ============================================================

class SymbolState:
    """
    记录每个合约的：
    - 精度信息（tick / qty_step / min_qty）
    - 实时盘口（best_bid / best_ask）
    - mid 价格 / 时间（用于振幅检测）
    - 当前挂单状态（id / side / price / ts）
    - 当前仓位名义（用于风控）
    - 近期成交数据（用于 taker-speed 分析）
    - 订单流方向EMA & 当前偏好方向（Bid / Ask）
    """

    def __init__(self, symbol: str):
        self.symbol = symbol

        # 精度
        self.tick: Decimal | None = None
        self.qty_step: Decimal | None = None
        self.min_qty: Decimal | None = None

        # 盘口
        self.best_bid: Decimal | None = None
        self.best_ask: Decimal | None = None

        # mid & 时间（用于短期振幅）
        self.last_mid: Decimal | None = None
        self.last_mid_ts: float | None = None

        # 当前挂出的订单（只保留一个 quote）
        self.active_order_id: str | None = None
        self.active_order_side: str | None = None   # "Bid" or "Ask"
        self.active_order_price: Decimal | None = None
        self.active_order_ts: float | None = None

        # 仓位名义
        self.position_notional: Decimal = Decimal("0")

        # taker 成交历史：deque[(ts, side, notional)]
        self.trades = deque()

        # 是否处于熔断冷静期
        self.cooldown_until: float = 0.0

        # 订单流 EMA & 当前“策略方向”
        self.imbalance_ema: Decimal | None = None
        self.preferred_side: str = "Bid"
        self.last_side_switch_ts: float = 0.0

    # ----------------盘口与 mid 更新----------------

    def update_mid(self):
        if self.best_bid is not None and self.best_ask is not None:
            mid = (self.best_bid + self.best_ask) / 2
            self.last_mid = mid
            self.last_mid_ts = time.time()

    # ----------------成交记录 & 不平衡----------------

    def record_trade(self, side: str, price: Decimal, qty: Decimal):
        """
        记录一笔成交，用于计算短期买卖不平衡 & taker 速度

        side:
            "Buy"  = taker 买入
            "Sell" = taker 卖出
        """
        notional = price * qty
        self.trades.append((time.time(), side, notional))
        cutoff = time.time() - TRADE_LOOKBACK_SEC
        while self.trades and self.trades[0][0] < cutoff:
            self.trades.popleft()

    def taker_imbalance(self) -> Decimal:
        """
        返回最近 TRADE_LOOKBACK_SEC 秒内的买卖不平衡比：
        buy_notional / sell_notional
        """
        buy_notional = Decimal("0")
        sell_notional = Decimal("0")
        for ts, side, notional in self.trades:
            if side == "Buy":
                buy_notional += notional
            elif side == "Sell":
                sell_notional += notional

        if sell_notional == 0 and buy_notional == 0:
            return Decimal("1")
        if sell_notional == 0:
            return Decimal("999")
        return buy_notional / sell_notional


MARKETS: dict[str, SymbolState] = {}
ACTIVE_SYMBOLS: list[str] = []


# ============================================================
#              动态选 Secondary Pairs（按量/深度/spread）
# ============================================================

async def select_symbols(client: httpx.AsyncClient):
    """
    使用官方：
      - GET /api/v1/markets?marketType=PERP 拿到所有永续合约列表
      - GET /api/v1/tickers 拿 24h volume/quoteVolume
      - 对高成交额的合约，用 /api/v1/depth 估算 spread 和深度
    """
    global ACTIVE_SYMBOLS

    try:
        # 1) 所有 PERP 合约
        resp_mk = await client.get(
            f"{API_BASE_URL}/api/v1/markets",
            params={"marketType": ["PERP"]},
            timeout=5,
        )
        resp_mk.raise_for_status()
        markets = resp_mk.json()
    except Exception as e:
        logging.error(f"获取 markets 失败: {e}")
        return

    perp_symbols: list[str] = []
    for m in markets:
        if m.get("marketType") != "PERP":
            continue
        if not m.get("visible", True):
            continue
        if m.get("orderBookState") != "Open":
            continue
        perp_symbols.append(m["symbol"])

    if not perp_symbols:
        logging.error("没有找到任何 PERP 合约")
        return

    # 2) 全部 24h tickers
    try:
        resp_tk = await client.get(f"{API_BASE_URL}/api/v1/tickers", timeout=5)
        resp_tk.raise_for_status()
        tickers = resp_tk.json()
    except Exception as e:
        logging.error(f"获取 tickers 失败: {e}")
        return

    # symbol -> quoteVolume
    vol_map: dict[str, Decimal] = {}
    for t in tickers:
        sym = t["symbol"]
        if sym in perp_symbols:
            try:
                vol = Decimal(t["quoteVolume"])
            except Exception:
                continue
            vol_map[sym] = vol

    # 先按 volume 预选一批，再看 spread & depth
    candidates_by_vol = [
        (s, v) for s, v in vol_map.items() if v >= MIN_24H_VOL
    ]
    if not candidates_by_vol:
        logging.error("按照 MIN_24H_VOL 没选出合约，建议先调低 MIN_24H_VOL。")
        return

    candidates_by_vol.sort(key=lambda x: x[1], reverse=True)
    shortlist = [s for s, _ in candidates_by_vol[:MAX_SYMBOLS * 5]]

    final_candidates: list[tuple[str, Decimal]] = []

    for sym in shortlist:
        try:
            resp_depth = await client.get(
                f"{API_BASE_URL}/api/v1/depth",
                params={"symbol": sym, "limit": "50"},
                timeout=5,
            )
            resp_depth.raise_for_status()
            ob = resp_depth.json()
        except Exception as e:
            logging.warning(f"[{sym}] 获取 depth 失败: {e}")
            continue

        bids = ob.get("bids") or []
        asks = ob.get("asks") or []
        if not bids or not asks:
            continue

        best_bid = Decimal(bids[0][0])
        best_ask = Decimal(asks[0][0])
        if best_bid <= 0 or best_ask <= 0:
            continue

        mid = (best_bid + best_ask) / 2
        spread = (best_ask - best_bid) / mid

        # 粗略计算前 DEPTH_LEVELS_FOR_FILTER 档买盘名义深度
        depth_notional = Decimal("0")
        for px_str, qty_str in bids[:DEPTH_LEVELS_FOR_FILTER]:
            px = Decimal(px_str)
            qty = Decimal(qty_str)
            depth_notional += px * qty

        if spread <= MAX_SPREAD_PCT and depth_notional >= MIN_DEPTH:
            final_candidates.append((sym, vol_map.get(sym, Decimal("0"))))

    if not final_candidates:
        logging.error("动态选币：按 spread/depth 过滤后为空，可以调松阈值。")
        return

    final_candidates.sort(key=lambda x: x[1], reverse=True)
    ACTIVE_SYMBOLS = [s for s, _ in final_candidates[:MAX_SYMBOLS]]
    logging.info(f"动态选币完成，本轮做市合约：{ACTIVE_SYMBOLS}")


# ============================================================
#                   精度 / 下单 / 撤单 / 仓位
# ============================================================

async def fetch_filters(client: httpx.AsyncClient, symbol: str):
    """
    读取单一合约的 tickSize / stepSize / minQty
    对应官方：GET /api/v1/market?symbol=...
    """
    st = MARKETS[symbol]
    try:
        resp = await client.get(
            f"{API_BASE_URL}/api/v1/market",
            params={"symbol": symbol},
            timeout=5,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logging.error(f"[{symbol}] 获取 market 失败: {e}")
        return

    price_filter = data["filters"]["price"]
    qty_filter = data["filters"]["quantity"]

    st.tick = Decimal(price_filter["tickSize"])
    st.qty_step = Decimal(qty_filter["stepSize"])
    st.min_qty = Decimal(qty_filter["minQuantity"])
    logging.info(f"[{symbol}] tick={st.tick}, qty_step={st.qty_step}, min_qty={st.min_qty}")


async def get_account_equity(client: httpx.AsyncClient) -> Decimal:
    """
    使用 /api/v1/capital/collateral 里的 netEquity 作为账户总权益。
    Instruction: collateralQuery
    """
    try:
        headers = auth_headers("collateralQuery")
        resp = await client.get(
            f"{API_BASE_URL}/api/v1/capital/collateral",
            headers=headers,
            timeout=5,
        )
        resp.raise_for_status()
        data = resp.json()
        return Decimal(data["netEquity"])
    except Exception as e:
        logging.error(f"获取账户权益失败（使用默认 1000U）: {e}")
        return Decimal("1000")


async def fetch_position_notional(client: httpx.AsyncClient, symbol: str) -> Decimal:
    """
    获取单一合约净仓位名义。
    GET /api/v1/position?symbol=...
    Instruction: positionQuery
    """
    try:
        params = {"symbol": symbol}
        headers = auth_headers("positionQuery", params)
        resp = await client.get(
            f"{API_BASE_URL}/api/v1/position",
            params=params,
            headers=headers,
            timeout=5,
        )
        if resp.status_code != 200:
            return Decimal("0")
        data = resp.json()
        if not data:
            return Decimal("0")
        pos = data[0]
        net_qty = Decimal(pos["netQuantity"])
        mark_price = Decimal(pos["markPrice"])
        return abs(net_qty * mark_price)
    except Exception as e:
        logging.error(f"[{symbol}] 获取仓位失败: {e}")
        return Decimal("0")


async def place_limit_order(
    client: httpx.AsyncClient,
    symbol: str,
    side: str,
    price: Decimal,
    qty: Decimal,
    post_only: bool = True,
    reduce_only: bool = False,
    tif: str = "GTC",
) -> str | None:
    """
    下一个普通限价单：
      side: "Bid" or "Ask"
    对应官方：POST /api/v1/order, Instruction: orderExecute
    """
    body = {
        "symbol": symbol,
        "side": side,
        "orderType": "Limit",
        "price": str(price),
        "quantity": str(qty),
        "timeInForce": tif,
        "postOnly": post_only,
        "reduceOnly": reduce_only,
    }
    headers = auth_headers("orderExecute", body)
    try:
        resp = await client.post(
            f"{API_BASE_URL}/api/v1/order",
            json=body,
            headers=headers,
            timeout=5,
        )
        resp.raise_for_status()
        data = resp.json()
        order_id = data["id"]
        logging.info(f"[{symbol}] 下单成功 side={side}, px={price}, qty={qty}, id={order_id}")
        return order_id
    except Exception as e:
        logging.error(f"[{symbol}] 下单失败: {e}")
        return None


async def cancel_all_open_orders(client: httpx.AsyncClient, symbol: str):
    """
    撤掉该合约的所有 RestingLimitOrder
    对应官方：DELETE /api/v1/orders, Instruction: orderCancelAll
    """
    st = MARKETS[symbol]
    body = {
        "symbol": symbol,
        "orderType": "RestingLimitOrder",
    }
    headers = auth_headers("orderCancelAll", body)
    try:
        await client.delete(
            f"{API_BASE_URL}/api/v1/orders",
            json=body,
            headers=headers,
            timeout=5,
        )
        st.active_order_id = None
        st.active_order_side = None
        st.active_order_price = None
        st.active_order_ts = None
        logging.info(f"[{symbol}] 已撤掉所有挂单")
    except Exception as e:
        logging.error(f"[{symbol}] 撤单失败: {e}")


# ============================================================
#                熔断风控：振幅 + spread
# ============================================================

def risk_triggered(st: SymbolState) -> bool:
    now = time.time()

    # 冷静期没过，直接认为风险中
    if now < st.cooldown_until:
        return True

    if st.best_bid is None or st.best_ask is None:
        return True

    mid = (st.best_bid + st.best_ask) / 2
    if mid <= 0:
        return True

    # 短期振幅
    if st.last_mid is not None and st.last_mid_ts is not None:
        dt = now - st.last_mid_ts
        if dt < 1.0:
            change = abs(mid - st.last_mid) / st.last_mid
            if change >= MAX_SHORT_VOLAT_PCT:
                logging.warning(f"[{st.symbol}] 熔断：1秒内振幅过大={change:.2%}")
                st.cooldown_until = now + COOLDOWN_SEC
                return True

    # spread 风险
    spread = (st.best_ask - st.best_bid) / mid
    if spread >= MAX_SPREAD_RISK:
        logging.warning(f"[{st.symbol}] 熔断：spread过大={spread:.2%}")
        st.cooldown_until = now + COOLDOWN_SEC
        return True

    return False


# ============================================================
#          仓位超限时的“限价反向平仓”逻辑（改进版）
# ============================================================

async def hedge_inventory(client: httpx.AsyncClient, symbol: str, st: SymbolState, equity: Decimal):
    """
    对冲逻辑要点：
    1）先通过 /position 获取真实 netQuantity（含方向）
    2）计算当前仓位名义 notional = |net_qty * mark|
    3）如果低于上限，不对冲
    4）只对冲“超额部分”的一部分（例如 50%），避免来回打满仓
    5）对冲用：
        - 多头超额：挂 Ask 在 best_bid 附近
        - 空头超额：挂 Bid 在 best_ask 附近
       使用限价 IOC，尽量吃到当前盘口，但避免挂残单太久
    """

    # 没有盘口不对冲
    if st.best_bid is None or st.best_ask is None:
        return

    # === Step 1: 获取真实仓位（方向 + 名义） ===
    try:
        params = {"symbol": symbol}
        headers = auth_headers("positionQuery", params)
        resp = await client.get(
            f"{API_BASE_URL}/api/v1/position",
            params=params,
            headers=headers,
            timeout=5,
        )
        resp.raise_for_status()
        data = resp.json()
        if not data:
            return
        pos = data[0]
        net_qty = Decimal(pos["netQuantity"])     # 正 = 多，负 = 空
        mark = Decimal(pos["markPrice"])
    except Exception as e:
        logging.error(f"[{symbol}] 对冲时获取仓位失败: {e}")
        return

    # 名义
    notional = abs(net_qty * mark)
    if notional <= 0:
        return

    # === Step 2: 根据“超额部分”决定对冲量 ===
    max_allowed = equity * MAX_EXPOSURE_PCT
    excess = notional - max_allowed

    # 只有超出上限才对冲
    if excess <= 0:
        return

    # 只对冲超出的部分（更合理）
    hedge_notional = excess * Decimal("0.5")   # 对冲 50% 超额（可调）
    # 最小对冲量限制
    if hedge_notional < Decimal("5"):
        return

    # === Step 3: 根据方向决定对冲方向 ===
    if net_qty > 0:
        # 多头 → 卖出（Ask）
        side = "Ask"
        ref_price = st.best_bid  # 挂在买一最容易成交
    else:
        # 空头 → 买入（Bid）
        side = "Bid"
        ref_price = st.best_ask  # 挂在卖一最容易成交

    # === Step 4: 换算成数量 ===
    if ref_price <= 0:
        return

    qty_raw = hedge_notional / ref_price
    qty = round_down_to_step(qty_raw, st.qty_step or Decimal("0.001"))
    if st.min_qty and qty < st.min_qty:
        return

    # === Step 5: 下限价 IOC（许多专业团队使用的方式） ===
    await place_limit_order(
        client,
        symbol,
        side=side,
        price=ref_price,
        qty=qty,
        post_only=False,
        reduce_only=True,
        tif="IOC",
    )

    logging.warning(
        f"[{symbol}] 对冲方向={side}, 仓位名义={notional}, "
        f"超额={excess}, 对冲名义={hedge_notional}, qty={qty}"
    )


# ============================================================
#     方向选择：基于 taker 订单流 + EMA + 滞回 + 最小持有时间
# ============================================================

def _update_imbalance_ema(st: SymbolState) -> Decimal:
    """
    用 EMA 平滑最近一段时间的买卖不平衡：
    ema = alpha * 当前值 + (1-alpha) * 旧 ema
    """
    raw = st.taker_imbalance()
    if st.imbalance_ema is None:
        st.imbalance_ema = raw
    else:
        st.imbalance_ema = (
            IMBALANCE_EMA_ALPHA * raw +
            (Decimal("1") - IMBALANCE_EMA_ALPHA) * st.imbalance_ema
        )
    return st.imbalance_ema


def choose_side(st: SymbolState) -> str:
    """
    返回当前应该挂单的方向："Bid" 或 "Ask"

    逻辑：
    1）先计算 taker 买/卖 notional 比例（买/卖）
    2）用 EMA 平滑，避免单个大单瞬时改变方向
    3）如果 EMA > IMBALANCE_THRESHOLD，说明买盘吃得多 → 我们挂 Ask（卖给他们）
       如果 EMA < 1/IMBALANCE_THRESHOLD，说明卖盘吃得多 → 我们挂 Bid
       中间区域视为“中性”，直接保持原方向
    4）加入最小持有时间 MIN_SIDE_HOLD_SEC，避免来回切方向抖动
    """
    now = time.time()
    ema = _update_imbalance_ema(st)

    if ema is None:
        return st.preferred_side

    upper = IMBALANCE_THRESHOLD
    lower = Decimal("1") / IMBALANCE_THRESHOLD

    # 先根据 EMA 得出“建议方向”
    if ema >= upper:
        suggested = "Ask"  # 买盘强 → 卖给他们
    elif ema <= lower:
        suggested = "Bid"  # 卖盘强 → 接他们的卖
    else:
        # 落在中性区间：直接保持原方向
        return st.preferred_side

    # 如果建议方向与当前方向不同，再看是否满足最小持有时间
    if suggested != st.preferred_side:
        if now - st.last_side_switch_ts >= MIN_SIDE_HOLD_SEC:
            logging.info(
                f"[{st.symbol}] 方向切换：{st.preferred_side} -> {suggested}, "
                f"imbalance_ema={ema:.2f}"
            )
            st.preferred_side = suggested
            st.last_side_switch_ts = now

    return st.preferred_side


# ============================================================
#                  单合约做市主循环
# ============================================================

async def maker_loop(symbol: str):
    st = MARKETS[symbol]

    async with httpx.AsyncClient() as client:
        # 初始化精度
        await fetch_filters(client, symbol)

        while True:
            await asyncio.sleep(0.1)

            # 没盘口 or 没精度，等待 WS/REST 初始化
            if st.best_bid is None or st.best_ask is None or st.tick is None:
                continue

            # 更新 mid 用于振幅风控
            st.update_mid()

            # 风控熔断：振幅 / spread
            if risk_triggered(st):
                await cancel_all_open_orders(client, symbol)
                continue

            # 账户权益 & 仓位名义
            equity = await get_account_equity(client)
            max_exposure = equity * MAX_EXPOSURE_PCT

            st.position_notional = await fetch_position_notional(client, symbol)

            # 如果已经超过最大敞口，上锁 + 对冲
            if st.position_notional >= max_exposure:
                logging.warning(
                    f"[{symbol}] 仓位名义 {st.position_notional} 超过最大敞口 {max_exposure}，执行对冲。"
                )
                await cancel_all_open_orders(client, symbol)
                await hedge_inventory(client, symbol, st, equity)
                continue

            # 通过订单流 EMA 决定当前策略方向
            side = choose_side(st)

            # 下单名义（封顶）
            target_notional = equity * ORDER_SIZE_PCT
            if target_notional > MAX_ORDER_NOTIONAL:
                target_notional = MAX_ORDER_NOTIONAL

            # 根据方向选择参考价格
            ref_price = st.best_bid if side == "Bid" else st.best_ask
            if ref_price <= 0:
                continue

            # 计算数量并按 step 向下取整
            raw_qty = target_notional / ref_price
            qty = round_down_to_step(raw_qty, st.qty_step or Decimal("0.001"))
            if st.min_qty and qty < st.min_qty:
                continue

            # 价格按 tick 偏移
            if side == "Bid":
                px = ref_price - st.tick * PRICE_OFFSET_TICKS
            else:
                px = ref_price + st.tick * PRICE_OFFSET_TICKS
            px = round_down_to_step(px, st.tick)

            now = time.time()

            # 当前没有挂单 → 直接挂
            if st.active_order_id is None:
                order_id = await place_limit_order(
                    client, symbol, side=side, price=px, qty=qty, post_only=True
                )
                if order_id:
                    st.active_order_id = order_id
                    st.active_order_side = side
                    st.active_order_price = px
                    st.active_order_ts = now
                continue

            # 如果行情偏离我们挂单价格 >= 1 tick，撤单重挂
            price_shift = abs(st.active_order_price - ref_price)
            if price_shift >= st.tick:
                await cancel_all_open_orders(client, symbol)
                continue

            # 如果挂单已经挂太久，撤单重挂
            if now - (st.active_order_ts or now) > MAX_ACTIVE_LIFETIME_SEC:
                await cancel_all_open_orders(client, symbol)
                continue


# ============================================================
#                 WebSocket：订单簿 + 成交数据
#              （自动重连 + 重新订阅 + 仅公有流）
# ============================================================

async def ws_loop():
    """
    使用官方 WS：
      - 订阅 bookTicker.<symbol> 拿 best bid/ask
      - 订阅 trade.<symbol> 拿成交（用 m 推导 taker 买卖方向）

    所有消息外层格式：{"stream": "...", "data": {...}}

    这里做了：
    - 自动重连（指数退避）
    - 每次重连后自动重新订阅当前 ACTIVE_SYMBOLS 的所有流
    """
    if not ACTIVE_SYMBOLS:
        logging.error("WS 启动失败：ACTIVE_SYMBOLS 为空")
        return

    # 要订阅的所有流
    def build_streams():
        streams: list[str] = []
        for sym in ACTIVE_SYMBOLS:
            streams.append(f"bookTicker.{sym}")
            streams.append(f"trade.{sym}")
        return streams

    backoff = 1  # 指数退避起始秒数

    while True:
        streams = build_streams()
        try:
            async with websockets.connect(
                WS_URL,
                ping_interval=60,
                ping_timeout=120,
            ) as ws:
                logging.info(f"WS 连接成功，订阅流: {streams}")

                sub_msg = {
                    "method": "SUBSCRIBE",
                    "params": streams,
                }
                await ws.send(json.dumps(sub_msg))

                # 连接成功则重置退避
                backoff = 1

                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue

                    # 官方所有流都有 {"stream": "...", "data": {...}} 外壳
                    data = msg.get("data", msg)
                    etype = data.get("e")
                    symbol = data.get("s")

                    if not symbol or symbol not in MARKETS:
                        continue
                    st = MARKETS[symbol]

                    if etype == "bookTicker":
                        # 订单簿最优价
                        st.best_bid = Decimal(data["b"])
                        st.best_ask = Decimal(data["a"])

                    elif etype == "trade":
                        # trade 流字段：
                        #  p: 价格, q: 数量, m: 是否 buyer 是 maker
                        #  我们要的是 taker 方向：
                        #   m=True  -> buyer 是 maker -> 卖方是 taker -> taker = "Sell"
                        #   m=False -> buyer 是 taker -> taker = "Buy"
                        price = Decimal(data["p"])
                        qty = Decimal(data["q"])
                        buyer_is_maker = bool(data["m"])
                        taker_side = "Sell" if buyer_is_maker else "Buy"
                        st.record_trade(taker_side, price, qty)

        except Exception as e:
            logging.error(f"WS 连接异常：{e}，将在 {backoff} 秒后重连...")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)  # 退避上限 60 秒


# ============================================================
#                    周期性动态选币任务
#         （目前只更新 ACTIVE_SYMBOLS，不热切换 loop）
# ============================================================

async def periodic_symbol_update():
    async with httpx.AsyncClient() as client:
        while True:
            await asyncio.sleep(600)  # 每 10 分钟尝试更新一次选币
            await select_symbols(client)
            logging.info("动态选币刷新完成（当前版本未热切换 maker_loop，仅更新 ACTIVE_SYMBOLS 配置）")


# ============================================================
#                         启动入口
# ============================================================

async def main():
    # 先跑一次选币
    async with httpx.AsyncClient() as client:
        await select_symbols(client)

    if not ACTIVE_SYMBOLS:
        logging.error("没有筛选出合适的合约，检查 MIN_24H_VOL / MAX_SPREAD_PCT / MIN_DEPTH 等参数。")
        return

    # 为每个合约创建状态
    for sym in ACTIVE_SYMBOLS:
        MARKETS[sym] = SymbolState(sym)

    # 启动 WS、maker 循环和选币更新任务
    ws_task = asyncio.create_task(ws_loop())
    maker_tasks = [asyncio.create_task(maker_loop(sym)) for sym in ACTIVE_SYMBOLS]
    symbol_update_task = asyncio.create_task(periodic_symbol_update())

    await asyncio.gather(ws_task, *maker_tasks, symbol_update_task)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Stopped by user.")
