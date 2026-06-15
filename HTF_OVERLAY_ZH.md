# htf_overlay.py

> 高级别周期（Higher-Timeframe）MACD 投影 + 顺势过滤。把高级别的 MACD **无未来函数地**投影到低级别时间轴，并按高级别方向过滤掉逆势的低级别背离。

一个单文件 Python 库，建立在 `indicator.py`（算 MACD）和 `divergence.py`（检测背离）之上，把"多周期共振"这条交易纪律变成可直接调用的数据结构：

- **高级别 MACD 投影** —— 把高级别每根 K 线的 MACD（hist / DIF / DEA）按时间映射到低级别网格，得到一条"阶梯式"的大级别动量背景。
- **无未来函数（no-lookahead）** —— 低级别每根只取*已收盘*的最近一根高级别 K 线的值，绝不把还在走的那根的未来值铺到过去。实时读取不会被"事后才好看"的形态欺骗。
- **高级别背离** —— 复用 `divergence.py`，在高级别上跑一遍三段背离 + 极值补检，箭头吸附回低级别网格。
- **顺势过滤** —— 按高级别 hist 的正负，逐根剔除与大级别方向相反的低级别背离（大级别向上删卖出、向下删买入）。

> **它不做什么**：不取数、不画图、不下单。只产出数据结构，I/O 与渲染由调用方负责——所以任何带 OHLC 的交易系统（实时盯盘、回测、机器人、其它 UI）都能直接 `import` 复用。

---

## 安装

单文件，依赖 `numpy` / `pandas`，外加同项目的两个库：

```bash
pip install numpy pandas
```

```
indicator.py    # add_indicators(df)：算 EMA / MACD
divergence.py   # find_three_segment_divergences / find_missed_extremes
```

把 `htf_overlay.py` 和上面两个文件放在同一可导入路径即可。`divergence.py` / `indicator.py` 的用法见 [LIBRARY_ZH](LIBRARY_ZH.md)。

---

## 快速开始

### A. 自带数据（回测 / 任意数据源）

```python
import htf_overlay as H

# base_times：低级别 K 线开盘时间(秒)，升序
# htf_candles：高级别 K 线 dict 列表(time/open/high/low/close)，升序
ov = H.project_htf(base_times, htf_candles, "15m")

if ov and ov["available"]:
    kept = H.trend_filter(my_markers, ov["macd"])   # 顺势过滤后的本级别信号
    # ov["macd"]    → 画高级别 MACD 叠加
    # ov["markers"] → 画高级别背离箭头
```

### B. 注入取数回调（实时）

```python
import htf_overlay as H

def my_fetch(symbol, interval, limit):
    # 返回 [{"time":秒,"open":..,"high":..,"low":..,"close":..}, ...]，升序
    ...

ov = H.fetch_and_project("NEXUSDT", base_times, interval="5m",
                         fetch=my_fetch, choice="auto")
```

`choice="auto"` 时按"梯子"取紧邻的上一档周期（见下表）。

---

## 核心概念：无未来函数

低级别每根 K 线**只取"收盘时间 ≤ 该根时间"的最近一根高级别 K 线的值**。所以同一根高级别 K 线在它存活期间内是一条水平线，**只有它收盘了，投影值才跳到下一格**。这条规则保证了你在 t 时刻看到的高级别状态，就是 t 时刻真正能拿来决策的状态——回测里不会引入 lookahead bias，实时盯盘也不会被"还在走的那根"骗。

---

## API

```python
import htf_overlay as H
```

### `resolve_htf(interval, choice="auto")`

解析参考（高级别）周期。`choice`：`"auto"`（走梯子）/ `"off"`（关）/ 显式周期（须严格高于 `interval`）。无更高 / 关 / 非法 → `None`。

### `project_htf(base_times, htf_candles, htf_interval, *, ...)`

纯计算核心，**零 I/O**。把高级别 MACD 投影到 `base_times`，并（可选）给出高级别背离。

| 参数 | 默认 | 含义 |
|------|------|------|
| `base_times` | — | 低级别 K 线开盘时间列表（秒），升序 |
| `htf_candles` | — | 高级别 K 线 dict 列表（time/open/high/low/close），升序 |
| `htf_interval` | — | 高级别周期串（用于推算每根收盘时间） |
| `min_macd_bars` | `35` | 高级别根数低于此值 → `available=False`（新币常见） |
| `with_divergences` | `True` | 是否计算高级别背离箭头 |
| `div_min_bars` | `0` | 透传给 `divergence.py`（建议与本级别一致） |
| `div_ratio_threshold` | `0.5` | 同上 |
| `div_max_level` | `None` | 同上（`None`=穷尽层级） |
| `block_by_opposite` | `True` | 同上 |

`base_times` 为空 / 无高级别 K 线 → 返回 `None`。

### `fetch_and_project(symbol, base_times, interval, fetch, *, choice="auto", fetch_limit=300, **project_kw)`

便捷封装：`resolve_htf` 解析 → 用注入的 `fetch(symbol, interval, limit)` 取数 → `project_htf` 投影。其余参数透传。无更高 / 关 → `None`；取数失败 / 数据不足 → `{"available": False}`。

### `trend_filter(base_markers, htf_macd, *, enabled=True)`

顺势过滤：按高级别 MACD hist 方向，**逐根**剔除逆势的低级别背离。

- `base_markers`：**你自己在本级别算出的背离**列表，每个至少含 `time`（与 `base_times` 同网格）和 `kind`（`'bullish'`/`'bearish'`）。
- `htf_macd`：`project_htf` 返回的 `macd` 列表。
- 规则：高级别 `hist>0` 删卖出（`kind!='bullish'`）；`hist<0` 删买入（`kind=='bullish'`）；该处无方向（缺失 / 0）保留。
- 返回**新列表**，不修改入参。

### 返回结构（`project_htf` / `fetch_and_project`）

| 字段 | 含义 |
|------|------|
| `interval` | 高级别周期串 |
| `available` | `False` = 数据不足/无效（新币常见），无投影 |
| `macd` | `[{time, hist, dif, dea} \| {time}]`，`time` 用 `base_times`；预热期/尚无已收盘高级别 K 线 → 只给 `{time}`（留空） |
| `markers` | `[{time, kind, level, provisional, double, missed}]`，`time` 已吸附到本级别网格 |

`markers` 字段与 `divergence.py` 语义一致：`kind` 为 `'bullish'`/`'bearish'`；`level` 为层级（1 基础、2+ 嵌套、**0 动量极值补检**）；`provisional` 末段未封口；`double` 多尺度共振；`missed` 是否极值补检。

---

## 示意图

```
本级别(1m):  │ │ │ │ │ │ │ │ │ │ │ │ │ │ │ │ │ │      每根一格
高级别(15m): └──── A ────┘└──── B ────┘└─── C ───┘     每根覆盖多格

投影到 1m :  …A A A A A A│B B B B B B│C C C…           段内水平
                         ▲          ▲
              15m K 线 A 收盘后值才跳到 B（无未来函数）
              ——还在走的那根，用的是上一根已收盘的值
```

读法：**看色块颜色与阶梯方向，不要跟本级别的零轴比高低**——大级别 hist 为正（绿）= 向上，为负（红）= 向下，贴零轴走平 = 中性。

---

## 常用模式

```python
import htf_overlay as H

# 回测：自带高级别数据 + 只保留确定信号 + 顺势过滤
ov = H.project_htf(base_times, htf_candles, "1h")
confirmed = [d for d in my_markers if not d.get("provisional")]
kept = H.trend_filter(confirmed, ov["macd"])

# 实时：注入交易所取数回调
ov = H.fetch_and_project("NEXUSDT", base_times, "5m", fetch=my_fetch)

# 关掉顺势过滤（核对它滤掉了哪些）
shown = H.trend_filter(my_markers, ov["macd"], enabled=False)

# 换梯子（默认是紧邻上一档；也可直接改 H.HTF_LADDER）
htf_iv = H.resolve_htf("1m", "auto", ladder={"1m": "1h"})
```

---

## 默认梯子（`choice="auto"`）

每个本级别取**紧邻的上一档**：

| 本级别 | 参考（高级别） |
|--------|----------------|
| 1m | 5m |
| 5m | 15m |
| 15m | 1h |
| 1h | 4h |
| 4h | 1d |
| 1d | 无（返回 `None`） |

---

## 参数调优速查

| 参数 | 默认 | 何时调整 |
|------|------|----------|
| `choice` | `"auto"` | 想跨两档看大势：传显式周期（如 1m 直接看 `"1h"`） |
| `min_macd_bars` | `35` | 高级别 K 线少（新币）想宽松：下调；但低于 ~30 根 MACD 本就不可靠 |
| `fetch_limit` | `300` | 覆盖区间不够 / 想要更长高级别历史：上调（受数据源上限约束） |
| `div_*` | 同 divergence | **务必与你本级别背离用同一套参数**，两级别才可比 |

---

## 边界与注意

- **新币数据不足**：算高级别 MACD 至少需 `min_macd_bars`（默认 35）根高级别 K 线。如 4h→1d 需约 35 天日线，刚上线的币返回 `{"available": False}`，调用方据此优雅降级。
- **1d 之上无更高周期**：`resolve_htf("1d","auto")` → `None`。
- **吸附**：高级别背离箭头按其价格极值那根的开盘时间，吸附到 `base_times` 里最近一根；落在区间外则丢弃。低级别周期应能整除高级别周期（梯子各档都满足），否则吸附会有一根的误差。
- **口径一致**：`div_*` 建议与你本级别背离检测用同一套参数。
- **不改入参**：`trend_filter` 返回新列表，不动你传进去的 markers。

---

## 文档

- **算法权威**：`htf_overlay.py` 模块顶部 docstring；冲突时以代码为准。

自测：`python htf_overlay.py`（跑梯子解析、投影含无未来函数校验、顺势过滤、新币数据不足等用例）。

---

## 免责声明

本库只产出结构化的指标 / 信号数据，**不构成交易建议**，也未做盈利性回测。基于其输出的任何决策由使用者自行承担。

## 协议

MIT。
