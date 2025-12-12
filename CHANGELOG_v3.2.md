# Backpack MM Tier Hunter v3.2 - 更新日志

## 版本概述
v3.2 版本专注于三大核心优化：API效率、完整日志记录和智能库存管理。

---

## 🎯 主要功能更新

### 1. API 优化 - 最大化利用 WebSocket
**目标**: 充分利用 2000/分钟 的 rate limit，减少 REST API 调用

**实现**:
- ✅ 所有 REST API 调用都添加了计数统计 (`GLOBAL_STATS.api_calls_count`)
- ✅ WebSocket 消息也进行计数统计 (`GLOBAL_STATS.ws_messages_count`)
- ✅ 保持原有缓存机制：
  - 权益查询：10秒缓存 (`EQUITY_UPDATE_INTERVAL = 10.0`)
  - 仓位查询：3秒缓存 (`POSITION_UPDATE_INTERVAL = 3.0`)
- ✅ 统计输出 WS/API 比率，监控效率

**优化位置**:
- `get_equity()` - 添加 API 计数
- `get_position()` - 添加 API 计数
- `place_order()` - 添加 API 计数
- `cancel_orders()` - 添加 API 计数
- `hedge_if_needed()` - 添加 API 计数
- `ws_handler()` - 添加 WS 消息计数

---

### 2. 完整日志系统 - 输出到 TXT 文件
**目标**: 记录所有关键交易指标到文件 `D:\ALLCRYPTO\backpack mm\pythonProject\log.txt`

**日志内容包括**:
```
- Maker/Taker 成交数量
- 总成交数量
- 失败订单数量
- 成功率
- Long/Short 比例（多空比）
- 当前权益
- 总盈亏 (Total PnL)
- 平均盈亏 (Average PnL)
- 最大盈利
- 最大亏损
- 总手续费
- REST API 调用次数
- WebSocket 消息数
- WS/API 比率
```

**实现**:
- ✅ 新增 `TradingStats` 类追踪所有统计数据
- ✅ 新增 `GLOBAL_STATS` 全局统计对象
- ✅ 新增 `write_stats_to_file()` 函数定期写入日志
- ✅ 日志间隔: 60秒 (`STATS_LOG_INTERVAL = 60.0`)
- ✅ 启动时初始化日志文件，记录配置信息

**配置**:
```python
LOG_FILE_PATH = r"D:\ALLCRYPTO\backpack mm\pythonProject\log.txt"
STATS_LOG_INTERVAL = 60.0  # 60秒输出一次
```

---

### 3. 智能库存管理 - 立即切换方向平仓
**目标**: 单边做市，一旦出现仓位立即切换方向用 limit order 平仓

**策略**:
1. **优先级1**: 检查是否有仓位（库存）
   - 有多仓 (position_qty > 0) → 立即切换到 Ask (卖出平仓)
   - 有空仓 (position_qty < 0) → 立即切换到 Bid (买入平仓)

2. **优先级2**: 无仓位时，使用原有订单流策略
   - 根据 taker 不平衡 (imbalance) 选择方向

**实现**:
- ✅ 修改 `choose_side()` 函数，添加库存优先逻辑
- ✅ 修改 `maker_loop()` 函数，有仓位时使用 `reduce_only=True`
- ✅ 新增配置参数:
  ```python
  INVENTORY_THRESHOLD = Decimal("0.001")  # 最小仓位阈值
  FORCE_REDUCE_ON_INVENTORY = True         # 强制库存管理
  ```
- ✅ `SymbolState` 类添加 `position_qty` 字段（带符号的净仓位）
- ✅ `get_position()` 函数现在同时保存仓位数量和名义价值

**日志示例**:
```
[SOL_USDC_PERP] 库存触发方向切换: Bid -> Ask, 仓位=0.1234, 名义价值=15.23
[SOL_USDC_PERP] 下单成功: Ask 0.1234@123.45, id=xxx, reduce=True
```

---

## 📊 新增类和函数

### `TradingStats` 类
全局交易统计类，追踪所有交易指标

**主要方法**:
- `record_fill(side, qty, price, is_maker)` - 记录成交
- `record_pnl(pnl)` - 记录盈亏
- `record_fee(fee)` - 记录手续费
- `get_avg_pnl()` - 获取平均盈亏
- `get_long_short_ratio()` - 获取多空比例
- `to_log_string(equity)` - 生成日志字符串

### `write_stats_to_file(equity)` 函数
将统计信息写入日志文件

---

## 🔧 配置变更

### 新增配置项
```python
# 日志文件路径
LOG_FILE_PATH = r"D:\ALLCRYPTO\backpack mm\pythonProject\log.txt"

# 统计日志输出间隔
STATS_LOG_INTERVAL = 60.0  # 60秒

# 库存管理
INVENTORY_THRESHOLD = Decimal("0.001")    # 最小仓位阈值
FORCE_REDUCE_ON_INVENTORY = True           # 强制库存切换
```

### 修改的配置项
```python
# 订单流驱动方向选择 + 库存管理
TRADE_LOOKBACK_SEC = 2.0
IMBALANCE_THRESHOLD = Decimal("1.3")
IMBALANCE_EMA_ALPHA = Decimal("0.4")
MIN_SIDE_HOLD_SEC = 1.5
```

---

## 🎨 代码改进

### 1. 日志系统升级
- 控制台和文件双输出
- 文件日志格式化，易于阅读
- 启动时记录配置信息

### 2. WebSocket 处理增强
- 统计 WS 消息数量
- 成交时更新全局统计
- 估算手续费（maker fee ~0.02%）

### 3. 订单管理优化
- 有仓位时自动使用 `reduce_only=True`
- 记录失败订单数量
- 增强日志输出（显示仓位数量）

---

## 📈 性能指标

### API 效率目标
- WS/API 比率应该 > 10:1（理想状态）
- 通过缓存，大幅减少权益和仓位查询
- 每分钟 API 调用应远小于 2000 次

### 统计输出频率
- 文件日志：每 60 秒
- 控制台统计：每 5 分钟
- 实时成交日志：即时输出

---

## 🚀 使用方法

### 1. 启动机器人
```bash
# 设置环境变量
set BPX_API_KEY=your_public_key_base64
set BPX_API_SECRET=your_private_key_base64

# 运行
python main.py
```

### 2. 查看日志
- **实时控制台**: 查看即时交易信息
- **文件日志**: `D:\ALLCRYPTO\backpack mm\pythonProject\log.txt`
  - 每60秒更新一次完整统计
  - 包含所有关键指标

### 3. 配置调整

**关闭库存管理** (恢复纯订单流策略):
```python
FORCE_REDUCE_ON_INVENTORY = False
```

**调整日志频率**:
```python
STATS_LOG_INTERVAL = 120.0  # 改为2分钟
```

**调整库存阈值**:
```python
INVENTORY_THRESHOLD = Decimal("0.01")  # 更高的阈值
```

---

## ⚠️ 注意事项

1. **日志文件路径**:
   - Windows 路径: `D:\ALLCRYPTO\backpack mm\pythonProject\log.txt`
   - Linux/Mac 需要修改为对应路径，例如: `/home/user/backpack_mm/log.txt`

2. **库存管理**:
   - 默认启用，确保单边做市
   - 一旦有仓位会立即切换方向
   - 使用 `reduce_only=True` 确保只平仓不开新仓

3. **手续费估算**:
   - 当前使用 0.02% 作为 maker fee 估算
   - 实际 Backpack maker fee 可能为负（返佣）
   - 可根据实际情况调整估算值

4. **PnL 计算**:
   - 当前版本进行基础的成交记录
   - 详细的已实现 PnL 需要配合订单成交 API
   - 后续版本可以添加更精确的 PnL 追踪

---

## 🔍 监控建议

### 关键指标
1. **WS/API 比率**: 应该 > 10，越高越好
2. **成功率**: (成交数 / (成交数 + 失败数)) 应该 > 90%
3. **多空平衡**: 单边做市情况下应接近 1:1
4. **库存水平**: 应保持接近 0（快速平仓）

### 异常告警
- API 调用频率接近 2000/分钟 → 检查缓存是否生效
- 成功率 < 80% → 检查网络或参数设置
- 库存持续不为 0 → 检查平仓逻辑是否正常

---

## 📝 后续优化建议

1. **精确 PnL 追踪**:
   - 使用 WebSocket 订单流或 REST API 查询成交历史
   - 计算真实的已实现盈亏

2. **动态手续费**:
   - 从 API 获取实际手续费率
   - 区分 maker/taker 费率

3. **风险指标**:
   - 添加夏普比率计算
   - 最大回撤追踪
   - 胜率统计

4. **告警系统**:
   - API 频率接近限制时告警
   - 库存超过阈值时告警
   - 连续失败订单告警

---

## 版本信息
- **版本**: v3.2
- **更新日期**: 2025-12-12
- **兼容性**: Python 3.7+
- **依赖**: httpx, websockets, cryptography

---

## 技术支持
如有问题，请检查：
1. 日志文件是否正常生成
2. API 调用统计是否合理
3. 库存管理是否按预期工作
4. 控制台输出是否有错误信息
