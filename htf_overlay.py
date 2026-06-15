"""
htf_overlay.py — 高级别周期 (Higher-Timeframe) MACD 投影 + 顺势过滤
================================================================

把高级别周期的 MACD 投影到低级别(本级别)时间轴，并按高级别方向过滤低级别
背离信号。**纯计算，不依赖 Flask / 任何特定数据源 / 任何前端。** 任何交易
系统(实时看盘、回测、机器人、其它 UI)都可直接 import 调用。

依赖
----
- numpy, pandas
- indicator.py   —— add_indicators(df) 计算 EMA / MACD
- divergence.py  —— find_three_segment_divergences / find_missed_extremes

K 线约定
--------
K 线为 dict 列表，至少含 'time'(开盘时间, 秒) / 'open' / 'high' / 'low' / 'close'，
按时间升序排列。

无未来函数 (no-lookahead)
-------------------------
低级别每根 K 线只取“收盘时间 <= 该根时间”的最近一根高级别 K 线的值，绝不把还在
走的那根高级别 K 线的未来值铺到过去。实时读取不会被“事后才知道”的形态欺骗。

公开接口
--------
- resolve_htf(interval, choice)                解析参考(高级别)周期 → 周期串 / None
- project_htf(base_times, htf_candles, htf_iv) 纯计算核心(零 I/O)
- fetch_and_project(symbol, base_times, interval, fetch)  便捷封装(注入取数回调)
- trend_filter(base_markers, htf_macd)         顺势过滤(剔除逆势的低级别信号)

最小用法
--------
    import htf_overlay as H

    # A. 自带数据(回测 / 任意数据源)：
    ov   = H.project_htf(base_times, htf_candles, "15m")
    kept = H.trend_filter(base_markers, ov["macd"])

    # B. 注入取数回调(实时)：
    #    my_fetch(symbol, interval, limit) -> list[candle dict]
    ov = H.fetch_and_project("NEXUSDT", base_times, "5m", fetch=my_fetch)
    if ov and ov["available"]:
        kept = H.trend_filter(base_markers, ov["macd"])

返回结构 (project_htf / fetch_and_project)
-----------------------------------------
    {
      "interval":  "15m",                       # 高级别周期
      "available": True/False,                  # False = 数据不足/无效，无投影(新币常见)
      "macd":      [{time, hist, dif, dea} | {time}],   # time 用 base_times；
                                                #   预热/尚无已收盘高级别 K 线 → 只给 {time}
      "markers":   [{time, kind, level, provisional, double, missed}],  # 高级别背离箭头
                                                #   time 已吸附到本级别网格；level=0 为动量极值补检
    }
"""
from __future__ import annotations

import bisect

import numpy as np
import pandas as pd

from indicator import add_indicators
from divergence import find_three_segment_divergences, find_missed_extremes


# 周期从低到高的顺序
INTERVALS_ORDER = ["1m", "5m", "15m", "1h", "4h", "1d"]

# 默认梯子 = 紧邻的上一档周期；1d 之上无更高周期。
HTF_LADDER = {"1m": "5m", "5m": "15m", "15m": "1h", "1h": "4h", "4h": "1d", "1d": None}

DEFAULT_FETCH_LIMIT   = 300   # 自动定量时的下限：足够 MACD 预热(>=35) + 给背离检测留足历史
DEFAULT_MIN_MACD_BARS = 35    # 高级别根数低于此值视为数据不足
MAX_FETCH_LIMIT       = 1000  # 自动定量时的上限(多数交易所 K 线接口单请求上限；可按数据源调整)


def interval_seconds(iv: str) -> int:
    """'1m' / '4h' / '1d' → 秒。"""
    return int(iv[:-1]) * {"m": 60, "h": 3600, "d": 86400}[iv[-1]]


def resolve_htf(interval, choice="auto", ladder=HTF_LADDER, intervals=INTERVALS_ORDER):
    """
    解析参考(高级别)周期。

    choice : 'auto'(走梯子) / 'off'(关) / 显式周期(必须严格高于 interval)
    返回   : 高级别周期字符串；'off' / 无更高 / 非法 → None
    """
    c = (choice or "auto").strip().lower()
    if c in ("off", "none", ""):
        return None
    if c == "auto":
        return ladder.get(interval)
    if (c in intervals and interval in intervals
            and interval_seconds(c) > interval_seconds(interval)):
        return c
    return None


def project_htf(base_times, htf_candles, htf_interval, *,
                min_macd_bars=DEFAULT_MIN_MACD_BARS,
                with_divergences=True,
                div_min_bars=0, div_ratio_threshold=0.5,
                div_max_level=None, block_by_opposite=True):
    """
    纯计算核心(零 I/O)：把高级别 MACD 投影到 base_times，并(可选)给出高级别背离箭头。

    参数
    ----
    base_times       : 低级别 K 线开盘时间列表(秒)，升序
    htf_candles      : 高级别 K 线 dict 列表(time/open/high/low/close)，升序
    htf_interval     : 高级别周期串(用于推算每根的收盘时间)
    min_macd_bars    : 高级别根数低于此值 → available False(新币常见)
    with_divergences : 是否计算高级别背离箭头
    div_*            : 透传给 divergence 库的参数(与本级别保持一致即可)

    base_times 为空 / 无高级别 K 线 → 返回 None。
    """
    if not base_times or not htf_interval or not htf_candles:
        return None
    if len(htf_candles) < min_macd_bars:
        return {"interval": htf_interval, "available": False, "macd": [], "markers": []}
    try:
        hdf = add_indicators(pd.DataFrame(htf_candles))
        hist, low, high = hdf["hist"], hdf["low"], hdf["high"]
    except Exception:
        return {"interval": htf_interval, "available": False, "macd": [], "markers": []}

    dur     = interval_seconds(htf_interval)
    open_t  = [c["time"] for c in htf_candles]      # 高级别开盘时间(秒)
    close_t = [t + dur for t in open_t]             # 高级别收盘时间
    hist_v  = hist.values
    dif_v   = hdf["macd"].values
    dea_v   = hdf["signal"].values
    n_htf   = len(htf_candles)

    # —— 无未来函数投影：本级别每根 t → 取“已收盘”的最近一根高级别 K 线 ——
    macd_proj = []
    for t in base_times:
        k = bisect.bisect_right(close_t, t) - 1
        if k < 0:
            macd_proj.append({"time": t})           # 还没有任何高级别 K 线收盘
            continue
        h = hist_v[k]
        if h != h:                                  # NaN：高级别 MACD 预热期
            macd_proj.append({"time": t})
            continue
        macd_proj.append({"time": t, "hist": float(h),
                          "dif": float(dif_v[k]), "dea": float(dea_v[k])})

    markers = []
    if with_divergences:
        try:
            divs = find_three_segment_divergences(
                hist, low, high, min_bars=div_min_bars,
                ratio_threshold=div_ratio_threshold,
                max_level=div_max_level, block_by_opposite=block_by_opposite)
            missed = find_missed_extremes(hist, low, high)
        except Exception:
            divs, missed = [], []

        b_min, b_max = base_times[0], base_times[-1]

        def _snap(open_time):
            # 高级别开盘时间 → 落在可视区内最近的本级别 K 线时间；超出区间则丢弃
            if open_time < b_min or open_time > b_max:
                return None
            j = bisect.bisect_right(base_times, open_time) - 1
            return base_times[j] if j >= 0 else None

        for d in divs:
            s, e = d["s3_start"], d["s3_end"]
            try:
                if d["kind"] == "bullish":
                    idx = s + int(np.nanargmin(low.iloc[s:e + 1].values))
                else:
                    idx = s + int(np.nanargmax(high.iloc[s:e + 1].values))
            except (ValueError, IndexError):
                continue
            if not (0 <= idx < n_htf):
                continue
            bt = _snap(open_t[idx])
            if bt is None:
                continue
            markers.append({"time": bt, "kind": d["kind"], "level": int(d["level"]),
                            "provisional": bool(d.get("provisional", False)),
                            "double": bool(d.get("same_terminal_l1", False)),
                            "missed": False})
        for d in missed:
            idx = d.get("peak_idx")
            if idx is None or not (0 <= idx < n_htf):
                continue
            bt = _snap(open_t[idx])
            if bt is None:
                continue
            markers.append({"time": bt, "kind": d["kind"], "level": 0,
                            "provisional": False, "double": False, "missed": True})
        markers.sort(key=lambda m: m["time"])

    return {"interval": htf_interval, "available": True,
            "macd": macd_proj, "markers": markers}


def _auto_fetch_limit(base_times, htf_interval,
                      min_macd_bars=DEFAULT_MIN_MACD_BARS, cap=MAX_FETCH_LIMIT):
    """
    自动定量高级别拉取根数：覆盖本级别可视区(base_times 跨度) + MACD 预热 + 余量；
    下限 DEFAULT_FETCH_LIMIT(给背离检测留足历史)，上限 cap(数据源单请求约束)。

    这样无论本级别拉多少根、是哪种周期组合，高级别都正好盖满 base_times，不会在左侧断开。
    """
    span = (base_times[-1] - base_times[0]) if (base_times and len(base_times) >= 2) else 0
    cover = span // interval_seconds(htf_interval) + 1     # 覆盖可视区所需高级别根数
    need = int(cover + min_macd_bars + 20)                 # + 预热 + 余量
    return max(DEFAULT_FETCH_LIMIT, min(need, cap))


def fetch_and_project(symbol, base_times, interval, fetch, *,
                      choice="auto", fetch_limit=None, **project_kw):
    """
    便捷封装：按梯子/选择解析高级别周期 → 用注入的 fetch 取数 → 投影。

    fetch       : 可调用对象 fetch(symbol, interval, limit) -> list[candle dict]
    fetch_limit : None = 自动定量(覆盖本级别可视区 + MACD 预热；下限 DEFAULT_FETCH_LIMIT，
                  上限 MAX_FETCH_LIMIT)；也可显式给定根数。
    choice / **project_kw 透传给 resolve_htf / project_htf。

    无更高周期 / 关 → None；取数失败 / 数据不足 → {available: False}。
    """
    htf_iv = resolve_htf(interval, choice)
    if not htf_iv:
        return None
    if fetch_limit is None:
        fetch_limit = _auto_fetch_limit(
            base_times, htf_iv, project_kw.get("min_macd_bars", DEFAULT_MIN_MACD_BARS))
    try:
        htf_candles = fetch(symbol, htf_iv, fetch_limit)
    except Exception:
        return {"interval": htf_iv, "available": False, "macd": [], "markers": []}
    return project_htf(base_times, htf_candles, htf_iv, **project_kw)


def trend_filter(base_markers, htf_macd, *, enabled=True):
    """
    顺势过滤：按高级别 MACD hist 方向，逐根剔除逆势的低级别背离。

    base_markers : 低级别背离 dict 列表，每个至少含 'time' 与 'kind'('bullish'/'bearish')
    htf_macd     : project_htf 返回的 'macd' 列表(用其 time -> hist)
    enabled      : False 时原样返回(不过滤)

    规则(逐根)：
        高级别向上(hist > 0) → 删卖出(kind != 'bullish')
        高级别向下(hist < 0) → 删买入(kind == 'bullish')
        该处无方向(hist 缺失 / == 0) → 保留
    返回新列表，不修改入参。
    """
    if not enabled or not htf_macd:
        return list(base_markers)
    hist = {m["time"]: m["hist"] for m in htf_macd if "hist" in m}
    out = []
    for d in base_markers:
        h = hist.get(d["time"])
        if h is None or h == 0:
            out.append(d)
            continue
        if h > 0 and d.get("kind") != "bullish":    # 大级别向上：删卖出(顶背离)
            continue
        if h < 0 and d.get("kind") == "bullish":     # 大级别向下：删买入(底背离)
            continue
        out.append(d)
    return out


# --------------------------------------------------------------------------- #
# 自测 / 演示：python htf_overlay.py
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import math

    def _demo_candles(interval, n):
        dur = interval_seconds(interval)
        out = []
        for i in range(n):
            px = 100 + 8 * math.sin(i / 4.0) + 0.05 * i
            out.append({"time": i * dur, "open": px, "high": px * 1.01,
                        "low": px * 0.99, "close": px, "volume": 1.0})
        return out

    print("resolve_htf:",
          {iv: resolve_htf(iv, "auto") for iv in INTERVALS_ORDER})
    print("resolve off / explicit / invalid:",
          resolve_htf("1m", "off"), resolve_htf("1m", "15m"), resolve_htf("1m", "1m"))

    htf = _demo_candles("5m", 60)
    base_times = list(range(50 * 300, 59 * 300, 60))     # 1m 网格，跨多根 5m
    ov = project_htf(base_times, htf, "5m")
    print(f"project_htf: available={ov['available']} "
          f"macd_pts={len(ov['macd'])} markers={len(ov['markers'])}")

    # 一根仍在走的 5m 中间点：应显示“上一根已收盘”的值(无未来函数)
    hist_v = add_indicators(pd.DataFrame(htf))["hist"].values
    mid = next(x for x in ov["macd"] if x["time"] == 55 * 300 + 120)
    assert abs(mid["hist"] - float(hist_v[54])) < 1e-9
    print("no-lookahead check ✓ (mid-bar shows previous CLOSED value)")

    fake = [{"time": 50 * 300, "kind": "bearish"}, {"time": 51 * 300, "kind": "bullish"}]
    kept = trend_filter(fake, ov["macd"])
    print(f"trend_filter: {len(fake)} -> {len(kept)} kept (drops counter-trend)")

    # 新币：高级别根数不足
    short = project_htf(base_times, htf[:20], "1d")
    print("short history:", {k: short[k] for k in ("interval", "available")})

    # fetch_and_project 自动定量：注入“返回最近 lim 根”的取数，验证左端被高级别盖住(不断层)
    NOW = 1_700_000_000
    def _fetch(sym, iv, lim):
        d = interval_seconds(iv)
        out = []
        for i in range(lim):
            px = 100 + 8 * math.sin(i / 4.0) + 0.05 * i
            out.append({"time": NOW - (lim - 1 - i) * d, "open": px,
                        "high": px * 1.01, "low": px * 0.99, "close": px})
        return out
    bt = [NOW - (1000 - 1 - i) * 300 for i in range(1000)]      # 1000 根 5m base
    print("auto fetch_limit (5m base 1000):",
          "->15m需", _auto_fetch_limit(bt, "15m"), " ->1h需", _auto_fetch_limit(bt, "1h"))
    ov2 = fetch_and_project("X", bt, "5m", _fetch, choice="auto")   # auto -> 15m
    blanks = sum(1 for m in ov2["macd"] if "hist" not in m)
    assert ov2["available"] and "hist" in ov2["macd"][0], "左端未被高级别覆盖"
    print(f"auto-cover 5m->15m: leading-blank={blanks} (应≈0) ✓")
    print("OK")
