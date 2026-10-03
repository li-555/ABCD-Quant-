# Roostoo 平台与策略接入

实现日期：2026-10-03。共享行情、策略输出和交易执行现已通过独立接口连接。

验证：完整离线测试 **100 项通过**，2 条警告来自旧研究模块的 pandas 弃用用法。覆盖签名、当前/旧余额字段、订单超时不重发、凭据隔离、纸面成交账本、预留资金、持久化去重、待成交订单拦截、跨批次日内上限、行情预热，以及 MultiFactor 最新行与历史前缀一致性。旧量价相关因子的索引对齐也已修复；这些修复不表示重新验证了旧策略收益。

```text
Roostoo ticker / exchangeInfo / balance
                  ↓
             RoostooData
                  ↓
      Strategy.generate(data)（可替换/组合）
                  ↓
      StrategySignal 或 TargetWeights
                  ↓
   StrategyBridge：验证、限仓、订单计划、执行账本
                  ↓
           RoostooClient
```

`bot/execution/client.py` 负责传输和签名；策略适配在 `bot/strategy_bridge.py`。根目录 `roostoo_client.py` 保留旧函数名，并提供 `get_market_context`、`run_strategy`、`submit_signal`，没有硬编码凭据。导入模块不会建立网络连接或下单。

## 实际连接验证

使用团队测试账户完成：交易所状态正常，exchangeInfo 返回 88 个交易对，ticker 返回 86 个行情，签名余额读取成功；外部示例策略成功生成订单计划。**发送/撤销订单数为 0，未使用比赛账户执行交易。** 各端点交易对数量可能不同，只能对实际有报价且允许交易的币种生成订单。

实际余额响应为 `SpotWallet` / `MarginWallet`。客户端将 `SpotWallet` 规范化成旧程序使用的 `Wallet`，不将保证金钱包混入现货权益。仍兼容旧 `Wallet` 响应。

## 凭据配置

测试账户环境变量：`ROOSTOO_TEST_API_KEY`、`ROOSTOO_TEST_API_SECRET`。

比赛账户环境变量：`ROOSTOO_COMPETITION_API_KEY`、`ROOSTOO_COMPETITION_API_SECRET`。

用 `ROOSTOO_ACCOUNT=test` / `competition` 或命令行 `--account` 显式选择。显式选择后，缺失凭据会报错，不借用另一个账户。旧通用变量仅在未显式选择账户且没有测试专用凭据时兼容。程序读取进程环境，**不会自动加载 `.env` 文件**；需要由 shell、服务管理器或部署平台注入。

不要把凭据提交到 YAML、代码、报告或 GitHub。配置对象的字符串表示会隐藏凭据。主分支先前存在的硬编码凭据即使在后续代码删除，仍可能留在公开提交历史中；需要由账户管理员在 Roostoo 端更换。不要把新密钥放回 Git 历史。

连接检查（不需要历史数据，不会发送订单）：

```bash
python -m bot.main --check-connection --account test
```

## 策略如何接收 Roostoo 数据

`RoostooData.snapshot()` 返回 `MarketContext`，包含行情、现货钱包、交易规则，以及提供给策略的宽表：

| 字段 | 含义 |
|---|---|
| `ticker_last` | 当前 LastPrice |
| `bid` / `ask` | 当前 MaxBid / MinAsk |
| `change_24h` | 平台 Change 字段 |
| `coin_trade_value` / `unit_trade_value` | 原始平台成交量/成交额字段，不转换成 K 线成交量 |
| `close` | 本地持续采集、按周期归档的已结束价格观察区间 |

索引使用 UTC，列为 `BTC`、`ETH` 等币种符号。当前报价保留服务器时间戳；`close` 排除尚未结束的区间，缺失区间不前填、不后填。观察数据存入 `cache/roostoo/observations.csv`，应由单一采集/调度进程写入。

**Roostoo 官方公开接口没有历史 OHLCV K 线接口。** 不能把 ticker 的成交量字段当作 30 分钟成交量，不能制造过去不存在的数据。旧 MultiFactor、Technical 以及 Volume 因子需要真正的逐周期成交量，因此不能仅凭 ticker 直接启用；需额外提供经验证且时间对齐的 OHLCV 数据，或实现只依赖平台已有字段的策略。这次未改变旧成交量因子的定义来伪装兼容。

旧 MultiFactor 信号适配器另有一个实时问题：它利用未来收益标签筛行，导致最新信号缺失。现已移除该依赖，离线研究因子计算仍可保留标签筛选模式；新增前缀一致性测试。

## 外部策略示例

```python
from quant_research.multi_strategy.strategy import StrategySignal

class MyStrategy:
    name = "my_strategy"
    required_fields = ("change_24h",)
    min_bars = 0

    def generate(self, data):
        scores = data["change_24h"].clip(-1, 1)
        return StrategySignal(self.name, scores)
```

保存到 `my_strategy.py` 后，以测试账户生成订单计划：

```bash
python -m bot.main --strategy my_strategy:MyStrategy --account test --pairs BTC/USD ETH/USD --once
```

也可以直接调用：

```python
from roostoo_client import run_strategy
result = run_strategy(MyStrategy(), ["BTC/USD", "ETH/USD"])
```

接口示例不代表通过回测的盈利策略。`StrategySignal` 是分数：负值在现货多头模式下转为 0，正分数按风险预算分配；可选 confidence 必须与信号逐项对齐且在 [0,1]。已有 `StrategyCombiner` 可在一个策略类的 `generate` 中合并多个策略，再交给同一个 Bridge；不要让多个独立策略争用同一个账户仓位。

如果策略已经算好了目标权重，应返回显式 `TargetWeights(name, asof, values)`，其中 values 为币种到 [0,1] 权重的 Series，权重基于扣除现金预留后的策略权益。没有出现的配置币种目标为 0；因此它代表完整目标组合，不是增量买卖指令。未知币种、非有限值、未来/过期时间戳会拒绝。若账户持有未配置币种，会拒绝交易而非遗漏其风险。

历史型策略可设置 `min_bars`。数据不足或 `generate` 返回 `None` 时，只采集数据、等待预热。`required_fields` 缺失会明确报错，不伪造字段。`python -m bot.platform` 另支持 `--interval` 调整轮询周期。

## 执行与恢复

默认只返回计划。实际平台执行同时要求 `LIVE=1` 与 `--execute`（Python 接口 `execute=True`）。本次没有开启这些开关。Bridge 使用单币/总仓位上限、最小调仓阈值、现金预留、可用余额、交易精度、最低订单金额和单笔金额上限。仅使用 MARKET 路径，逐笔成交后重新读取账户，先卖后买；不会调用旧的 LIMIT 取消后立即全量重下路径。

这套通用 Bridge 的风控与 v1 完整交易规则不同，**不会自动给外部策略套用 v1 的趋势门槛、波动目标、止损和回撤熔断**。需要这些规则的策略应在输出目标权重前实现并验证；不要把 v1 历史绩效直接归因给新策略或新执行器。

下单接口无文档化幂等键，因此订单发送失败/超时不自动重发。SQLite 账本位于 `cache/roostoo_<account>_orders.sqlite3`：发送前保存批次与订单尝试，收到响应后保存订单 ID/状态；相同策略与信号时间戳不重复提交。日内订单上限跨批次保存。未完成、超时、拒绝、待成交或缺失成交数量会标记 attention，阻止后续批次。操作员需先通过 Roostoo 查询订单并核对余额，再处理账本；不要删除账本后盲目重试。不同进程必须共享同一个账户账本，运行中只保留一个账户写入者。

## Bot v1 使用 Roostoo 行情

```bash
# 先在环境变量中设置 LIVE=0
python -m bot.main --config bot/config/roostoo_paper.yaml
```

该模式从 Roostoo 采集报价、在本地模拟成交。历史不足时继续记录，而不是在采集之前退出。需要先积累最长指标窗口所需的有效历史；ticker 观察历史不是交易所完整 K 线，不能保证复现 Binance 历史回测。默认 v1 的 Binance 来源保持可选。Volume 需要逐周期成交量，因此 Roostoo-only 示例明确关闭它。

官方协议来源：[Roostoo API Documents](https://github.com/roostoo/Roostoo-API-Documents)。GET ticker 只需要 timestamp，签名用于余额和交易接口。
