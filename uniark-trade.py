#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
uniark-trade 短线王 + MACD 背离（只读，无需 API Key，不涉及任何下单）
=====================================================================
两个看盘列表：
  · 自选 —— 主流现货 USDT 交易对(BTC/ETH/BNB/SOL/DOGE)，走币安现货公开接口
  · Alpha —— mulPoint == 4 的 Alpha 代币，按成交额(盘口量)排序，估算 4 倍剩余天数
点任意一行 → 右边出 K 线（蜡烛）+ MACD 副面板（hist/DIF/DEA）+ 背离三角 + 高级别叠加。
两个市场共用同一套 MACD / 背离 / 高级别投影管线（indicator.py / divergence.py / htf_overlay.py）。

依赖：  pip install flask requests pandas numpy
放置：  本文件需与 indicator.py / divergence.py / htf_overlay.py 放在同一目录
运行：  python uniark-trade.py            (默认端口 5000)
        python uniark-trade.py 8080       (指定端口)
浏览器：http://127.0.0.1:5000   或   http://<局域网IP>:5000

颜色：默认「绿涨红跌」(币圈/西方习惯)。
      前端 RED_UP 开关可一键翻成「红涨绿跌」(国内习惯)。
说明：后端服务器侧请求币安公开行情接口，无浏览器 CORS 问题。
"""

import time
import json
import requests
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, jsonify, request, Response

# ---- 接入用户的 MACD / 背离核心模块（需与本文件同目录）----
# 任一缺失 → 静默降级：终端照常显示 K 线，但没有 MACD / 背离。
try:
    import numpy as np
    import pandas as pd
    from indicator import add_indicators
    from divergence import find_three_segment_divergences, find_missed_extremes
    import htf_overlay as htfmod
    DIVERGENCE_OK = True
    _DIV_ERR = ""
except Exception as _e:                       # noqa: BLE001
    DIVERGENCE_OK = False
    _DIV_ERR = str(_e)

BASE = "https://www.binance.com"
TOKEN_LIST_URL = f"{BASE}/bapi/defi/v1/public/wallet-direct/buw/wallet/cex/alpha/all/token/list"
KLINES_URL     = f"{BASE}/bapi/defi/v1/public/alpha-trade/klines"
TICKER_URL     = f"{BASE}/bapi/defi/v1/public/alpha-trade/ticker"   # Alpha 盘口 24h 行情(quoteVolume = 币安App「成交额」)
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

# ---- 自选(现货)数据源：币安现货公开接口（裸数组返回，K 线数组结构与 Alpha 端点一致）----
# 若所在网络访问 api.binance.com 受限，改这一行即可(镜像：api1.binance.com / data-api.binance.vision)。
SPOT_BASE       = "https://api.binance.com"
SPOT_KLINES_URL = f"{SPOT_BASE}/api/v3/klines"
SPOT_TICKER_URL = f"{SPOT_BASE}/api/v3/ticker/24hr"
WATCHLIST       = ["BTC", "ETH", "BNB", "SOL", "DOGE"]   # 自选：主流现货 USDT 交易对(固定顺序)

BONUS_WINDOW_DAYS = 30        # 官方规则：空投/TGE 后 30 天内享加成

# 盘口成交额(quoteVolume)抓取与缓存 —— 逐币拉 Alpha 盘口 ticker，覆盖列表的链上量
TURNOVER_WORKERS = 10         # 并发线程数
TURNOVER_TIMEOUT = 6          # 单个 ticker 请求超时(秒)
TURNOVER_TTL     = 45         # 盘口量缓存秒数，避免频繁刷新重复打 ticker
_TURNOVER_CACHE  = {}         # alphaId -> (ts, quoteVolume)

# 背离扫描参数（按你的项目语义）
DIV_MIN_BARS        = 0       # 关闭短段合并，靠层级递归吸收噪声
DIV_RATIO_THRESHOLD = 0.5     # 面积比阈值
DIV_MAX_LEVEL       = None    # None = 穷尽所有层级（充分发挥分层递归）
MIN_BARS_FOR_MACD   = 35      # 不足这么多根不算（MACD 预热）

# 高级别(参考)周期叠加：梯子 / 解析 / 投影 / 顺势过滤 已抽到独立库 htf_overlay.py，
# 任何交易系统都可直接 import 复用；本看板通过 htfmod 调用(见 /api/klines)。

app = Flask(__name__)


# ----------------------------- 数据层 -----------------------------
def fetch_tokens():
    r = requests.get(TOKEN_LIST_URL, headers=HEADERS, timeout=12)
    r.raise_for_status()
    j = r.json()
    if not j.get("success"):
        raise RuntimeError(f"{j.get('code')} {j.get('message')}")
    return j.get("data", [])


def _f(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def fetch_quote_volume(alpha_id):
    """拉单个 Alpha 盘口 24h ticker 的 quoteVolume(USDT 成交额 = 币安App「成交额」)；失败返回 None。"""
    try:
        r = requests.get(TICKER_URL, headers=HEADERS,
                         params={"symbol": f"{alpha_id}USDT"}, timeout=TURNOVER_TIMEOUT)
        r.raise_for_status()
        j = r.json()
        if not j.get("success"):
            return None
        qv = (j.get("data") or {}).get("quoteVolume")
        return float(qv) if qv is not None else None
    except Exception:                              # noqa: BLE001
        return None


def quote_volume_cached(alpha_id):
    """带 TTL 缓存的盘口成交额；缓存未命中才打网络。取不到时退回上次缓存(若有)。"""
    now = time.time()
    hit = _TURNOVER_CACHE.get(alpha_id)
    if hit and now - hit[0] < TURNOVER_TTL:
        return hit[1]
    qv = fetch_quote_volume(alpha_id)
    if qv is not None:
        _TURNOVER_CACHE[alpha_id] = (now, qv)
        return qv
    return hit[1] if hit else None                 # 网络失败：用旧缓存兜底


def build_4x(tokens):
    """筛出 4 倍代币，算估算剩余天数。volume24h 先占位为链上量，盘口量在 enrich 阶段覆盖并重排。"""
    now_ms = time.time() * 1000
    out = []
    for t in tokens:
        if _f(t.get("mulPoint"), 1) < 4:        # 只要 4 倍
            continue
        listing_ms = _f(t.get("listingTime"), 0)
        listing_date, est_days = None, None
        if listing_ms > 0:
            listing_date = time.strftime("%Y-%m-%d", time.localtime(listing_ms / 1000))
            days_since = (now_ms - listing_ms) / 86_400_000
            est_days = round(BONUS_WINDOW_DAYS - days_since, 1)   # 可能为负(老币重获加成)
        out.append({
            "symbol":      t.get("symbol"),
            "name":        t.get("name"),
            "alphaId":     t.get("alphaId"),
            "pair":        f'{t.get("alphaId") or ""}USDT',   # 交易对(喂给 K 线接口)；前端按 pair 做唯一键
            "market":      "alpha",                           # 市场标记：alpha=Alpha 盘口 / spot=现货
            "chain":       t.get("chainName"),
            "mulPoint":    int(_f(t.get("mulPoint"), 1)),
            "liquidity":   _f(t.get("liquidity")),
            "price":       _f(t.get("price")),
            "change24h":   _f(t.get("percentChange24h")),
            "chainVol":    _f(t.get("volume24h")),   # 链上/钱包量(token列表)，仅作兜底/备查
            "volume24h":   _f(t.get("volume24h")),   # 成交额：先占位为链上量，稍后用盘口量覆盖
            "volSource":   "chain",                  # 成交额来源：chain=链上兜底 / alpha=盘口
            "listingDate": listing_date,
            "estDaysLeft": est_days,
        })
    out.sort(key=lambda x: x["volume24h"], reverse=True)
    return out


def enrich_with_turnover(rows):
    """用 Alpha 盘口 quoteVolume(=币安App成交额)覆盖各行 volume24h，并按成交额降序重排。
       盘口取不到的行退回链上量(volSource=chain)。"""
    if not rows:
        return rows

    def _fill(row):
        qv = quote_volume_cached(row["alphaId"])
        if qv is not None:
            row["volume24h"] = qv
            row["volSource"] = "alpha"
        else:
            row["volume24h"] = row.get("chainVol", row["volume24h"])
            row["volSource"] = "chain"
        return row

    with ThreadPoolExecutor(max_workers=min(TURNOVER_WORKERS, len(rows))) as ex:
        list(ex.map(_fill, rows))
    rows.sort(key=lambda x: x["volume24h"], reverse=True)
    return rows


def fetch_klines(symbol, interval, limit, market="alpha"):
    """拉 K 线。
       market='alpha' 走 Alpha 盘口端点（{success,data} 包裹）；
       market='spot'  走币安现货 /api/v3/klines（裸数组）。
       两者的 K 线数组结构一致(0 开盘时间ms /1 开 /2 高 /3 低 /4 收 /5 量 /7 成交额 /8 笔数)，复用同一解析。"""
    if market == "spot":
        r = requests.get(SPOT_KLINES_URL, headers=HEADERS,
                         params={"symbol": symbol, "interval": interval, "limit": limit}, timeout=12)
        r.raise_for_status()
        rows = r.json()
        if not isinstance(rows, list):                 # 现货异常一般回 {"code":..,"msg":..}
            raise RuntimeError(str(rows)[:200])
    else:
        r = requests.get(KLINES_URL, headers=HEADERS,
                         params={"symbol": symbol, "interval": interval, "limit": limit}, timeout=12)
        r.raise_for_status()
        j = r.json()
        if not j.get("success"):
            raise RuntimeError(f"{j.get('code')} {j.get('message')}")
        rows = j.get("data", [])
    candles = []
    for k in rows:
        candles.append({
            "time":     int(int(k[0]) / 1000),   # 秒，喂给 lightweight-charts
            "open":     _f(k[1]),
            "high":     _f(k[2]),
            "low":      _f(k[3]),
            "close":    _f(k[4]),
            "volume":   _f(k[5]),
            "quoteVol": _f(k[7]),                 # USDT 成交额
            "trades":   int(_f(k[8])),
        })
    return candles


def fetch_watchlist():
    """批量拉取自选现货 24h ticker（一次请求覆盖全部），按 WATCHLIST 固定顺序返回行。
       字段对齐代币行：symbol / pair / market='spot' / price / change24h / volume24h(USDT 成交额)。"""
    pairs = [s + "USDT" for s in WATCHLIST]
    params = {"symbols": json.dumps(pairs, separators=(",", ":"))}   # 紧凑 JSON，无空格(币安要求)
    r = requests.get(SPOT_TICKER_URL, headers=HEADERS, params=params, timeout=12)
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, list):
        raise RuntimeError(str(data)[:200])
    by_pair = {d.get("symbol"): d for d in data}
    rows = []
    for s in WATCHLIST:
        pair = s + "USDT"
        d = by_pair.get(pair)
        if not d:
            continue
        rows.append({
            "symbol":    s,
            "name":      s,
            "pair":      pair,
            "market":    "spot",
            "price":     _f(d.get("lastPrice")),
            "change24h": _f(d.get("priceChangePercent")),
            "volume24h": _f(d.get("quoteVolume")),   # USDT 成交额(与 Alpha 列表口径一致)
            "baseVol":   _f(d.get("volume")),        # 基础币成交量(备查)
        })
    return rows


# ----------------------------- MACD + 背离 -----------------------------
def compute_macd_and_divergences(candles):
    """
    对一组 K 线算 MACD 并检测背离，返回 (macd_list, markers)。

    macd_list : [{time, dif, dea, hist}, ...]（预热期那几根只给 {time}，
                作为 whitespace 让 MACD 面板与 K 线面板时间轴对齐）
    markers   : [{time, kind, level, ratio, provisional, double, missed}, ...]
                time = 该背离锚定的那根 K 线（顶=区间最高价那根 / 底=最低价那根；
                补检极值 level=0 用 peak_idx）。

    模块缺失或 K 线过少 → 返回 ([], [])，终端只画纯 K 线。
    """
    if not DIVERGENCE_OK or len(candles) < MIN_BARS_FOR_MACD:
        return [], []
    try:
        df = pd.DataFrame(candles)
        df = add_indicators(df)                       # 追加 macd / signal / hist
        hist, low, high = df["hist"], df["low"], df["high"]
    except Exception:
        return [], []

    # MACD 面板序列
    macd_list = []
    for i in range(len(df)):
        h = df["hist"].iat[i]
        if pd.isna(h):
            macd_list.append({"time": candles[i]["time"]})        # whitespace
        else:
            macd_list.append({
                "time": candles[i]["time"],
                "dif":  float(df["macd"].iat[i]),
                "dea":  float(df["signal"].iat[i]),
                "hist": float(h),
            })

    # 背离检测（主检测 + 漏检极值补检）
    try:
        divs = find_three_segment_divergences(
            hist, low, high,
            min_bars=DIV_MIN_BARS,
            ratio_threshold=DIV_RATIO_THRESHOLD,
            max_level=DIV_MAX_LEVEL,
            block_by_opposite=True,
        )
        missed = find_missed_extremes(hist, low, high)
    except Exception:
        return macd_list, []

    n = len(candles)
    markers = []
    for d in divs:
        s, e = d["s3_start"], d["s3_end"]
        # 锚点 = 最末同向段 S_last 内的价格极值那根
        try:
            if d["kind"] == "bullish":
                idx = s + int(np.nanargmin(low.iloc[s:e + 1].values))
            else:
                idx = s + int(np.nanargmax(high.iloc[s:e + 1].values))
        except (ValueError, IndexError):
            continue
        if not (0 <= idx < n):
            continue
        markers.append({
            "time":        candles[idx]["time"],
            "kind":        d["kind"],
            "level":       int(d["level"]),
            "ratio":       float(d.get("ratio", 0.0)),
            "provisional": bool(d.get("provisional", False)),
            "double":      bool(d.get("same_terminal_l1", False)),
            "missed":      False,
        })
    for d in missed:
        idx = d.get("peak_idx")
        if idx is None or not (0 <= idx < n):
            continue
        markers.append({
            "time":        candles[idx]["time"],
            "kind":        d["kind"],
            "level":       0,
            "ratio":       0.0,
            "provisional": False,
            "double":      False,
            "missed":      True,
        })
    markers.sort(key=lambda m: m["time"])
    return macd_list, markers


# ----------------------------- 路由 -----------------------------
@app.route("/")
def index():
    return Response(HTML, mimetype="text/html")


@app.route("/api/tokens")
def api_tokens():
    try:
        rows = enrich_with_turnover(build_4x(fetch_tokens()))     # 盘口成交额覆盖 + 按成交额重排
        matched = sum(1 for r in rows if r.get("volSource") == "alpha")
        return jsonify({"ok": True, "tokens": rows, "ts": int(time.time()),
                        "volMatched": matched, "volTotal": len(rows)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502


@app.route("/api/klines")
def api_klines():
    symbol   = request.args.get("symbol", "").strip()
    interval = request.args.get("interval", "1m").strip()
    market   = request.args.get("market", "alpha").strip().lower()
    if market not in ("alpha", "spot"):
        market = "alpha"
    try:
        limit = max(1, min(1000, int(request.args.get("limit", 500))))   # 端点单次上限 1000
    except ValueError:
        limit = 500
    if not symbol:
        return jsonify({"ok": False, "error": "missing symbol"}), 400
    try:
        candles = fetch_klines(symbol, interval, limit, market)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502
    macd, markers = compute_macd_and_divergences(candles)
    # 高级别(参考)周期叠加：解析(梯子/显式/关) + 拉取 + 投影 全部交给 htf_overlay 库。
    # 取数回调注入本模块的 fetch_klines，并锁定同一 market(自选→现货 / Alpha→盘口)。
    htf_fetch = lambda s, iv, lim: fetch_klines(s, iv, lim, market)
    htf = (htfmod.fetch_and_project(
               symbol, [c["time"] for c in candles], interval, htf_fetch,
               choice=request.args.get("htf", "auto"),
               min_macd_bars=MIN_BARS_FOR_MACD, div_min_bars=DIV_MIN_BARS,
               div_ratio_threshold=DIV_RATIO_THRESHOLD, div_max_level=DIV_MAX_LEVEL)
           if DIVERGENCE_OK else None)
    return jsonify({"ok": True, "candles": candles, "macd": macd,
                    "divergences": markers, "htf": htf, "divOk": DIVERGENCE_OK})


@app.route("/api/watchlist")
def api_watchlist():
    """自选(现货)列表：BTC/ETH/BNB/SOL/DOGE 的 24h 行情。失败不影响 Alpha 标签。"""
    try:
        rows = fetch_watchlist()
        return jsonify({"ok": True, "tokens": rows, "ts": int(time.time())})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502


@app.route("/api/ticker")
def api_ticker():
    """调试用：直接回传某 symbol 的盘口 24h ticker 原始 JSON，方便核对 quoteVolume。
       例：/api/ticker?symbol=ALPHA_162USDT"""
    symbol = request.args.get("symbol", "").strip()
    if not symbol:
        return jsonify({"ok": False, "error": "missing symbol, e.g. ALPHA_162USDT"}), 400
    try:
        r = requests.get(TICKER_URL, headers=HEADERS, params={"symbol": symbol}, timeout=TURNOVER_TIMEOUT)
        r.raise_for_status()
        return jsonify({"ok": True, "raw": r.json()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502


# ----------------------------- 前端 -----------------------------
HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, viewport-fit=cover">
<title>uniark-trade 短线王</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=Syne:wght@700;800&display=swap" rel="stylesheet">
<script src="https://unpkg.com/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"></script>
<style>
:root{
  --bg:#0a0b0d; --panel:#101216; --panel2:#15181d; --border:#23272e;
  --text:#e6e8eb; --muted:#7d848e; --dim:#565c66;
  --accent:#e8b339; --accent-dim:#8a6a1f;
  --up:#1fbf8f; --down:#ef5350; --selbg:#1b1e24;
}
*{box-sizing:border-box}
html,body{height:100%}
body{
  margin:0; background:var(--bg); color:var(--text);
  font-family:'IBM Plex Mono',ui-monospace,monospace; font-size:13px;
  -webkit-text-size-adjust:100%;
  background-image:repeating-linear-gradient(0deg,transparent,transparent 2px,rgba(255,255,255,.012) 2px,rgba(255,255,255,.012) 3px);
}
.app{display:flex; flex-direction:column; height:100vh; height:100dvh}

/* top bar */
.topbar{display:flex; align-items:center; gap:18px; padding:12px 18px;
  padding-top:calc(12px + env(safe-area-inset-top));
  border-bottom:1px solid var(--border); background:linear-gradient(180deg,#0e1014,#0a0b0d)}
.brand{font-family:'Syne',sans-serif; font-weight:800; font-size:19px; letter-spacing:.5px; white-space:nowrap}
.brand .hl{color:var(--accent)}
.tag{font-size:10px; color:var(--accent); border:1px solid var(--accent-dim);
  padding:2px 7px; border-radius:3px; letter-spacing:1px; white-space:nowrap}
.spacer{flex:1}
.meta{font-size:11px; color:var(--muted); white-space:nowrap}
.meta b{color:var(--text); font-weight:600}
.btn{background:var(--panel2); border:1px solid var(--border); color:var(--text);
  font-family:inherit; font-size:11px; padding:6px 11px; border-radius:4px; cursor:pointer; transition:.15s; white-space:nowrap}
.btn:hover{border-color:var(--accent-dim); color:var(--accent)}

/* main split */
.main{flex:1; display:flex; min-height:0}
.left{width:42%; max-width:560px; min-width:340px; border-right:1px solid var(--border);
  display:flex; flex-direction:column; min-height:0}
.right{flex:1; display:flex; flex-direction:column; min-height:0}
/* 列表标签页：自选 / Alpha */
.tabbar{display:flex; gap:2px; padding:7px 10px 0; border-bottom:1px solid var(--border);
  background:linear-gradient(180deg,#0e1014,#0a0b0d)}
.tab{flex:0 0 auto; background:transparent; border:none; color:var(--muted);
  font-family:inherit; font-size:13px; font-weight:600; letter-spacing:.5px;
  padding:8px 16px; margin-bottom:-1px; cursor:pointer; border-bottom:2px solid transparent; transition:.15s}
.tab:hover{color:var(--text)}
.tab.on{color:var(--accent); border-bottom-color:var(--accent)}
.panel-h{padding:9px 16px; font-size:11px; color:var(--muted); letter-spacing:1px;
  border-bottom:1px solid var(--border); display:flex; align-items:center; gap:8px; text-transform:uppercase}

/* token list (cards) — 保留 流动性 / 估算剩余 / 链 / 倍数 等 4X 特征信息 */
.listwrap{overflow-y:auto; flex:1; -webkit-overflow-scrolling:touch}
.tok{display:flex; align-items:center; gap:13px; padding:11px 16px;
  border-bottom:1px solid #16191e; cursor:pointer; transition:background .1s}
.tok:hover{background:#13161b}
.tok:active{background:#171b21}
.tok.sel{background:var(--selbg); box-shadow:inset 3px 0 0 var(--accent)}
.tok-rank{color:var(--dim); font-size:12px; min-width:18px; text-align:right; flex:none}
.tok-id{flex:1; min-width:0}
.tok-r1{display:flex; align-items:baseline; gap:8px}
.tok-sym{font-weight:700; font-size:15px; letter-spacing:.3px}
.tok-sub{color:var(--dim); font-size:11px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap}
.tok-r2{display:flex; align-items:center; gap:6px; margin-top:5px}
.badge-mult{font-size:9.5px; color:var(--accent); border:1px solid var(--accent-dim);
  border-radius:3px; padding:1px 5px; font-weight:600; flex:none}
.badge-chain{font-size:9.5px; color:var(--muted); background:var(--panel2); border:1px solid var(--border);
  border-radius:3px; padding:1px 6px; letter-spacing:.3px; flex:none}
.tok-vals{text-align:right; flex:none}
.tok-vol{font-size:14px; font-weight:600}                        /* 成交额(盘口·主) */
.tok-liq{font-size:12px; color:var(--muted); margin-top:3px}     /* 流动性(次) */
.tok-days{font-size:13px; margin-top:4px}
.vlabel{color:var(--dim); font-weight:400; font-size:10px; margin-right:4px}  /* 指标小标签 */
.d-ok{color:var(--up)} .d-warn{color:var(--accent)} .d-over{color:#c98b2a} .d-na{color:var(--dim)}
.up{color:var(--up)} .down{color:var(--down)}

/* chart panel */
.chart-head{padding:12px 18px; border-bottom:1px solid var(--border)}
.ch-top{display:flex; align-items:center; gap:12px; flex-wrap:wrap}
.back-btn{display:none; align-items:center; justify-content:center; width:34px; height:34px;
  background:var(--panel2); border:1px solid var(--border); color:var(--text);
  border-radius:7px; font-size:18px; cursor:pointer; flex:none; padding:0; line-height:1}
.back-btn:active{background:#1b1f25}
.ch-sym{font-family:'Syne',sans-serif; font-weight:800; font-size:22px}
.ch-id{color:var(--muted); font-size:11px}
.ch-price{font-size:18px; font-weight:600}
.ch-pg{margin-left:auto; display:flex; align-items:baseline; gap:8px}
.ch-chg{font-size:13px; font-weight:600}
.ch-stats{display:flex; gap:22px; margin-top:9px; font-size:11px; color:var(--muted); flex-wrap:wrap}
.ch-stats b{color:var(--text); font-weight:600; margin-left:5px}
.ctrl-row{display:flex; align-items:center; justify-content:space-between; gap:12px; margin-top:11px; flex-wrap:wrap}
.intervals{display:flex; gap:6px; flex-wrap:wrap}
.iv{background:var(--panel2); border:1px solid var(--border); color:var(--muted);
  font-family:inherit; font-size:11px; padding:5px 12px; border-radius:4px; cursor:pointer; transition:.12s}
.iv:hover{color:var(--text)}
.iv.on{background:var(--accent); border-color:var(--accent); color:#1a1408; font-weight:600}
.htf-sel{background:var(--panel2); border:1px solid var(--border); color:var(--muted);
  font-family:inherit; font-size:11px; padding:5px 8px; border-radius:4px; cursor:pointer; transition:.12s}
.htf-sel:hover{color:var(--text); border-color:var(--accent-dim)}
.htf-sel:disabled{opacity:.45; cursor:not-allowed}
.macd-bar .htf-leg{color:var(--accent)}
.ctrl-right{display:flex; align-items:center; gap:16px}
.sig{font-size:11px; color:var(--muted)} .sig b{color:var(--text); margin-left:4px; font-weight:600}
.mk{display:flex; align-items:center; gap:6px; font-size:11px; color:var(--muted); cursor:pointer; user-select:none}
.mk input{accent-color:var(--accent); cursor:pointer; margin:0; width:15px; height:15px}
.price-wrap{flex:1 1 62%; min-height:0; position:relative}
#price{position:absolute; inset:0}
.ma-legend{position:absolute; top:6px; left:12px; z-index:3; display:flex; gap:14px; flex-wrap:wrap;
  font-size:10px; color:var(--muted); pointer-events:none; letter-spacing:.3px}
.ma-legend .leg{display:flex; align-items:center; gap:5px}
.ma-legend .dot{width:11px; height:2px; display:inline-block; border-radius:1px}
.ma-legend b{color:var(--text); font-weight:500}
#macd{flex:1 1 30%; min-height:0; position:relative}
.macd-bar{display:flex; align-items:center; gap:14px; padding:5px 18px; flex-wrap:wrap;
  border-top:1px solid var(--border); font-size:10px; color:var(--muted); letter-spacing:.3px}
.macd-bar .macd-title{color:var(--dim); letter-spacing:1px; text-transform:uppercase}
.macd-bar .leg{display:flex; align-items:center; gap:5px}
.macd-bar .dot{width:11px; height:2px; display:inline-block; border-radius:1px}
.macd-bar b{color:var(--text); font-weight:500}

/* banners */
.err{display:none; background:#2a1416; border:1px solid #5b2327; color:#ff9a9a;
  padding:8px 18px; font-size:12px}
.flag{display:none; color:var(--accent); font-size:11px}

/* footer */
.foot{padding:8px 18px; border-top:1px solid var(--border); font-size:10.5px;
  color:var(--dim); line-height:1.6; padding-bottom:calc(8px + env(safe-area-inset-bottom))}
.foot b{color:var(--muted)}
.foot-min{display:none}

/* scrollbar */
::-webkit-scrollbar{width:9px;height:9px}
::-webkit-scrollbar-thumb{background:#262a31; border-radius:5px}
::-webkit-scrollbar-track{background:transparent}
@keyframes fade{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}
.main{animation:fade .35s ease both}
@keyframes slidein{from{opacity:0;transform:translateX(12px)}to{opacity:1;transform:none}}

/* ---------- 手机 ---------- */
@media (max-width: 768px){
  body{font-size:14px}
  .topbar{padding:10px 14px; padding-top:calc(10px + env(safe-area-inset-top)); gap:10px}
  .brand{font-size:16px}
  .tag, .meta{display:none}
  #btn-refresh-t{display:none}
  .main{flex-direction:column; animation:none}
  .left{width:100%; max-width:none; min-width:0; border-right:none}
  .left, .right{display:none}
  body:not(.m-chart) .left{display:flex}
  body.m-chart .right{display:flex; animation:slidein .25s ease both}
  .back-btn{display:inline-flex}
  .panel-h{padding:11px 16px}
  .tab{padding:10px 18px; font-size:14px}
  .tok{padding:13px 16px}
  .tok-sym{font-size:16px}
  .tok-vol{font-size:15px}
  .tok-liq{font-size:13px}
  .tok-days{font-size:14px}
  .chart-head{padding:10px 14px}
  .ch-sym{font-size:20px}
  .ch-price{font-size:17px}
  .ch-stats{gap:13px; margin-top:8px}
  .ctrl-row{margin-top:10px}
  .iv{padding:7px 13px; font-size:12px}
  .ch-stats{display:none}
  body.m-chart .topbar{display:none}
  body.m-chart .chart-head{padding-top:calc(10px + env(safe-area-inset-top))}
  body.m-chart .foot{display:none}
  /* 控件栏在手机上拆成两行：周期按钮一行；下拉 + 两个开关并排占第二行 */
  .ctrl-row{flex-direction:column; align-items:stretch; gap:10px}
  .intervals{flex:none}
  .ctrl-right{flex:none; width:100%; justify-content:flex-start; flex-wrap:nowrap; align-items:center; gap:0 12px; margin-top:2px}
  .htf-sel{flex:1 1 auto; min-width:0}      /* 下拉可压缩，与开关并排同一行；过长则自身截断 */
  .ctrl-right .mk{flex:none; white-space:nowrap}   /* 开关不压缩，保持完整 */
  .sig{display:none}
  .price-wrap{flex:2.4 1 0}
  #macd{flex:1 1 0}
  .foot{font-size:10px; padding:7px 14px; text-align:center;
    padding-bottom:calc(7px + env(safe-area-inset-bottom))}
  .foot-full{display:none}
  .foot-min{display:inline}
}
</style>
</head>
<body>
<div class="app">
  <div class="topbar">
    <div class="brand">uniark<span class="hl">-trade</span> <span id="brand-word">短线王</span></div>
    <span class="tag" id="tag-ro">只读</span>
    <div class="spacer"></div>
    <div class="meta"><span id="meta-tok">4倍代币</span> <b id="count">–</b> · <span id="meta-upd">刷新于</span> <b id="refresh-time">–</b></div>
    <button class="btn" id="btn-refresh" onclick="loadActive()">↻ <span id="btn-refresh-t">刷新</span></button>
    <button class="btn" id="btn-lang" onclick="toggleLang()">EN</button>
  </div>

  <div class="err" id="err"></div>

  <div class="main">
    <!-- 左：4倍代币列表 -->
    <div class="left">
      <div class="tabbar" id="tabbar"></div>
      <div class="panel-h" id="left-h">4倍代币 · 按成交额排序</div>
      <div class="listwrap" id="listwrap"></div>
    </div>

    <!-- 右：K 线 + MACD -->
    <div class="right">
      <div class="chart-head">
        <div class="ch-top">
          <button class="back-btn" id="back-btn" onclick="showList()" aria-label="返回列表">←</button>
          <span class="ch-sym" id="ch-sym">—</span>
          <span class="ch-id" id="ch-id"></span>
          <span class="flag" id="ch-flag"></span>
          <span class="ch-pg">
            <span class="ch-price" id="ch-price"></span>
            <span class="ch-chg" id="ch-chg">—</span>
          </span>
        </div>
        <div class="ch-stats">
          <span><span id="lbl-liq">流动性</span><b id="ch-liq">—</b></span>
          <span><span id="lbl-vol">24h量</span><b id="ch-vol">—</b></span>
          <span><span id="lbl-listed">上线</span><b id="ch-listed">—</b></span>
          <span><span id="lbl-days">估算剩余</span><b id="ch-days">—</b></span>
        </div>
        <div class="ctrl-row">
          <div class="intervals" id="intervals"></div>
          <div class="ctrl-right">
            <select id="htf-select" class="htf-sel" onchange="onHtfChange()" title="高级别参考周期叠加"></select>
            <span class="sig"><span id="sig-label">信号</span> <b id="sig-count">0</b></span>
            <label class="mk"><input type="checkbox" id="mk-toggle" checked onchange="toggleMarkers()"><span id="mk-label">背离标记</span></label>
            <label class="mk" title="高级别 MACD 向上时隐藏本级别卖出信号，向下时隐藏买入信号（需先开启高级别叠加）"><input type="checkbox" id="tf-toggle" checked onchange="onTrendFilterChange()"><span id="tf-label">顺势过滤</span></label>
          </div>
        </div>
      </div>
      <div class="price-wrap">
        <div class="ma-legend">
          <span class="leg"><i class="dot" style="background:#ff9900"></i>MA7 <b id="ma7-val">–</b></span>
          <span class="leg"><i class="dot" style="background:#cc44ff"></i>MA25 <b id="ma25-val">–</b></span>
          <span class="leg"><i class="dot" style="background:#00aaff"></i>MA99 <b id="ma99-val">–</b></span>
        </div>
        <div id="price"></div>
      </div>
      <div class="macd-bar">
        <span class="macd-title">MACD</span>
        <span class="leg"><i class="dot" style="background:#26c6da"></i>DIF <b id="dif-val">–</b></span>
        <span class="leg"><i class="dot" style="background:#ffa726"></i>DEA <b id="dea-val">–</b></span>
        <span class="leg">MACD <b id="hist-val">–</b></span>
        <span class="leg htf-leg" id="htf-leg" style="display:none"><span id="htf-leg-label">高级别</span> <b id="htf-leg-iv">–</b></span>
      </div>
      <div id="macd"></div>
    </div>
  </div>

  <div class="foot" id="foot"></div>
</div>

<script>
const INTERVALS = ['1m','5m','15m','1h','4h','1d'];
// 各周期基础 K 线根数：低级别多拉，避免“窗口太短、往左拉很快到头”（Alpha 端点单次上限 1000）。
const BASE_LIMITS = { '1m':500, '5m':500 };   // 未列出的周期沿用默认 500
const S = { alphaTokens:[], watchTokens:[], tab:'watch', selected:null, interval:'1m', lang:'zh', markers:[], showMarkers:true, lastMA:null, lastMACD:null,
            htf:'auto', htfInterval:null, htfMarkers:[], htfMacd:[], trendFilter:true };
// 当前标签页对应的列表：自选 → 现货 / Alpha → 4倍代币
function currentList(){ return S.tab==='alpha' ? S.alphaTokens : S.watchTokens; }

/* 涨跌 / 背离标记配色。RED_UP=false 绿涨红跌(币圈/西方习惯)；改 true 即国内红涨绿跌。 */
const RED_UP = false;
const C_UP   = RED_UP ? '#ef5350' : '#26a69a';   // 涨 K 线
const C_DOWN = RED_UP ? '#26a69a' : '#ef5350';   // 跌 K 线
const C_BULL = RED_UP ? '#ff5252' : '#2ebd85';   // 底背离(看涨)标记
const C_BEAR = RED_UP ? '#2ebd85' : '#ff5252';   // 顶背离(看跌)标记
const C_PROV = '#1e90ff';                          // 未完成(provisional)警示
const C_HTF      = '#e8b339';                      // 高级别背离标记(琥珀，区别于本级别红绿)
const C_HTF_DIM  = 'rgba(232,179,57,.55)';         // 高级别未完成(provisional)

let priceChart, candleS, ma7S, ma25S, ma99S, macdChart, histS, difS, deaS;
let htfHistS, htfDifS, htfDeaS;

const isMobile = () => window.matchMedia('(max-width: 768px)').matches;

/* ---------- 中英文对照 ---------- */
const I18N = {
  zh:{
    title:'uniark-trade 短线王', brand:'短线王', tabWatch:'自选', tabAlpha:'Alpha', spot:'现货', leftHWatch:'自选 · 主流现货', readonly:'只读', langBtn:'EN', refreshT:'刷新',
    metaTok:'4倍代币', metaUpd:'刷新于',
    leftHAlpha:'4倍代币 · 按成交额排序',
    lblLiq:'流动性', lblVol:'24h成交额', lblListed:'上线', lblDays:'估算剩余',
    lblLiqS:'流动', lblVolS:'成交', lblDaysS:'剩余', lblPriceS:'价', lblChgS:'涨跌',
    active:'进行中', activeTitle:'上线超30天仍为x4，疑似新一轮空投重新触发加成', activeLong:'进行中(>30d)',
    notX4:'⚠ 已不在 4 倍列表', mkLabel:'背离标记', sigLabel:'信号', tfLabel:'顺势过滤',
    htfLabel:'高级别', htfAuto:'参考高级别MACD·自动', htfOff:'参考高级别MACD·关', htfNA:'数据不足',
    err:m=>'⚠ 拉取失败：'+m+'（检查网络/能否访问 binance.com）',
    foot:'数据源：币安公开行情接口（bapi，只读，无需 API Key）。<b>「估算剩余」= 30 − 距上线天数，仅供参考</b>：官方 30 天加成从空投/TGE 当天起算，接口未暴露该时间，故对「重新获得加成的老币」显示为“进行中(&gt;30d)”。<b>背离标记</b>：绿▲=底背离(看涨)/红▼=顶背离(看跌)，蓝=未完成(?)，L+数字=层级(L0=动量极值补检)，+号=多尺度共振；信号为辅助参考，非交易建议。<b>琥珀 ▲▼</b>=高级别(参考)周期背离；MACD 面板浅色柱/线=高级别 MACD 投影(无未来函数，仅在高级别 K 线收盘后才更新)。本工具仅做行情监控，不下单、不自动交易。',
    footMin:'只读监控 · 不下单 · 信号仅供参考，非交易建议',
  },
  en:{
    title:'uniark-trade · Short-Swing', brand:'Short-Swing', tabWatch:'Watchlist', tabAlpha:'Alpha', spot:'Spot', leftHWatch:'Watchlist · Spot USDT', readonly:'READ-ONLY', langBtn:'中', refreshT:'Refresh',
    metaTok:'×4 tokens', metaUpd:'updated',
    leftHAlpha:'×4 Tokens · sorted by turnover',
    lblLiq:'Liquidity', lblVol:'24h Turnover', lblListed:'Listed', lblDays:'Est. left',
    lblLiqS:'LIQ', lblVolS:'VOL', lblDaysS:'Left', lblPriceS:'Price', lblChgS:'Chg',
    active:'active', activeTitle:'Listed over 30 days but still ×4 — likely re-qualified via a new airdrop round', activeLong:'active(>30d)',
    notX4:'⚠ no longer ×4', mkLabel:'Divergence', sigLabel:'signals', tfLabel:'Trend filter',
    htfLabel:'HTF', htfAuto:'Ref HTF MACD · auto', htfOff:'Ref HTF MACD · off', htfNA:'no data',
    err:m=>'⚠ Fetch failed: '+m+' (check network / access to binance.com)',
    foot:'Data: Binance public market endpoints (bapi, read-only, no API key). <b>"Est. left" = 30 − days since listing, reference only</b>: the official 30-day bonus counts from the airdrop/TGE date, which the API does not expose, so re-qualified older tokens show "active(&gt;30d)". <b>Markers</b>: green ▲ = bullish / red ▼ = bearish, blue = provisional (?), L+number = level (L0 = momentum-extreme supplement), + = multi-scale resonance; signals are indicative, not trading advice. <b>Amber ▲▼</b> = higher-timeframe divergence; faint bars/lines in the MACD panel = projected higher-timeframe MACD (no lookahead — updates only after the HTF candle closes). Read-only monitoring — no trading.',
    footMin:'Read-only · no trading · signals are indicative only',
  },
};
const T = ()=>I18N[S.lang];
const setText=(id,txt)=>{const el=document.getElementById(id); if(el) el.textContent=txt;};

function applyLang(){
  const t=T();
  document.title=t.title;
  document.documentElement.lang = S.lang==='zh'?'zh-CN':'en';
  setText('brand-word',t.brand); setText('tag-ro',t.readonly);
  setText('meta-upd',t.metaUpd);
  setText('btn-refresh-t',t.refreshT); setText('btn-lang',t.langBtn);
  renderTabbar(); updateTabUI();
  setText('lbl-liq',t.lblLiq); setText('lbl-vol',t.lblVol);
  setText('lbl-listed',t.lblListed); setText('lbl-days',t.lblDays);
  setText('mk-label',t.mkLabel); setText('sig-label',t.sigLabel); setText('tf-label',t.tfLabel);
  setText('htf-leg-label',t.htfLabel); renderHtfSelect();
  const f=document.getElementById('foot');
  if(f) f.innerHTML='<span class="foot-full">'+t.foot+'</span><span class="foot-min">'+t.footMin+'</span>';
  renderTable(); updateHeader();
}
function toggleLang(){
  S.lang = S.lang==='zh'?'en':'zh';
  try{ localStorage.setItem('alpha4x_lang', S.lang); }catch(e){}
  applyLang();
}

const fmtUSD   = n => '$' + Math.round(n).toLocaleString('en-US');
/* 紧凑金额：中文 亿/万、英文 B/M/K，贴近币安成交额显示 */
function fmtCompact(n){
  if(n==null || isNaN(n)) return '–';
  const v=Math.abs(n), d2={minimumFractionDigits:2, maximumFractionDigits:2};
  if(S.lang==='zh'){
    if(v>=1e8) return '$'+(n/1e8).toLocaleString('en-US',d2)+'亿';
    if(v>=1e4) return '$'+(n/1e4).toLocaleString('en-US',d2)+'万';
    return '$'+Math.round(n).toLocaleString('en-US');
  }
  if(v>=1e9) return '$'+(n/1e9).toLocaleString('en-US',d2)+'B';
  if(v>=1e6) return '$'+(n/1e6).toLocaleString('en-US',d2)+'M';
  if(v>=1e3) return '$'+(n/1e3).toLocaleString('en-US',d2)+'K';
  return '$'+Math.round(n).toLocaleString('en-US');
}
const fmtPrice = p => p===0?'0':p<0.001?p.toFixed(8):p<1?p.toFixed(6):p<100?p.toFixed(4):p.toFixed(2);
const precOf   = p => p<0.001?8:p<1?6:p<100?4:2;
function fmtVal(v){
  if(v==null || isNaN(v)) return '–';
  const a=Math.abs(v);
  if(a===0) return '0';
  if(a>=100) return v.toFixed(2);
  if(a>=1)   return v.toFixed(4);
  if(a>=0.01)return v.toFixed(5);
  return v.toFixed(7);
}

function showErr(msg){
  const e=document.getElementById('err');
  if(msg){e.textContent=T().err(msg); e.style.display='block';}
  else{e.style.display='none';}
}

function daysCell(d){
  if(d===null||d===undefined) return '<span class="d-na">—</span>';
  if(d<=0)  return '<span class="d-over" title="'+T().activeTitle+'">'+T().active+'</span>';
  if(d<=5)  return '<span class="d-warn">'+d+'d</span>';
  return '<span class="d-ok">'+d+'d</span>';
}

/* 代币列表（卡片）。Alpha：保留 倍数/链/流动性/估算剩余；自选：现货精简卡(价/成交/涨跌)。
   两类卡片统一用 pair(完整交易对，如 BTCUSDT / ALPHA_162USDT)做唯一键。 */
function renderTable(){
  const box=document.getElementById('listwrap');
  const list=currentList();
  box.innerHTML = list.map((t,i)=> S.tab==='alpha' ? alphaCard(t,i) : watchCard(t,i)).join('');
  [...box.querySelectorAll('.tok')].forEach(el=>{
    el.onclick=()=>{ const t=currentList().find(x=>x.pair===el.dataset.id); if(t) selectToken(t, true); };
  });
  setText('count', list.length);
}

function alphaCard(t,i){
  const sel = S.selected && S.selected.pair===t.pair ? ' sel' : '';
  return `<div class="tok${sel}" data-id="${t.pair}">
      <span class="tok-rank">${i+1}</span>
      <div class="tok-id">
        <div class="tok-r1"><span class="tok-sym">${t.symbol||''}</span><span class="tok-sub">${t.alphaId||''}</span></div>
        <div class="tok-r2"><span class="badge-mult">×${t.mulPoint||4}</span><span class="badge-chain">${t.chain||''}</span></div>
      </div>
      <div class="tok-vals">
        <div class="tok-vol"><span class="vlabel">${T().lblVolS}</span>${fmtCompact(t.volume24h)}</div>
        <div class="tok-liq"><span class="vlabel">${T().lblLiqS}</span>${fmtCompact(t.liquidity)}</div>
        <div class="tok-days"><span class="vlabel">${T().lblDaysS}</span>${daysCell(t.estDaysLeft)}</div>
      </div>
    </div>`;
}

function watchCard(t,i){
  const sel = S.selected && S.selected.pair===t.pair ? ' sel' : '';
  const chg = (t.change24h>=0?'+':'') + (t.change24h!=null?t.change24h.toFixed(2):'0.00') + '%';
  const dir = t.change24h>=0?'up':'down';
  return `<div class="tok${sel}" data-id="${t.pair}">
      <span class="tok-rank">${i+1}</span>
      <div class="tok-id">
        <div class="tok-r1"><span class="tok-sym">${t.symbol||''}</span><span class="tok-sub">${T().spot}</span></div>
        <div class="tok-r2"><span class="badge-chain">${t.pair||''}</span></div>
      </div>
      <div class="tok-vals">
        <div class="tok-vol"><span class="vlabel">${T().lblPriceS}</span>$${fmtPrice(t.price)}</div>
        <div class="tok-liq"><span class="vlabel">${T().lblVolS}</span>${fmtCompact(t.volume24h)}</div>
        <div class="tok-days"><span class="vlabel">${T().lblChgS}</span><span class="${dir}">${chg}</span></div>
      </div>
    </div>`;
}

function updateHeader(){
  const t=S.selected; if(!t) return;
  setText('ch-sym', t.symbol||'—');
  setText('ch-id', (t.market==='spot')
      ? (t.pair||'')+' · '+T().spot
      : (t.alphaId||'')+' · '+(t.chain||''));
  setText('ch-price', t.price?fmtPrice(t.price):'');
  const chg=document.getElementById('ch-chg');
  chg.textContent=(t.change24h>=0?'+':'')+t.change24h.toFixed(2)+'%';
  chg.className='ch-chg '+(t.change24h>=0?'up':'down');
  setText('ch-liq', fmtCompact(t.liquidity));
  setText('ch-vol', fmtCompact(t.volume24h));
  setText('ch-listed', t.listingDate||'—');
  const dEl=document.getElementById('ch-days');
  if(t.estDaysLeft===null||t.estDaysLeft===undefined) dEl.innerHTML='—';
  else if(t.estDaysLeft<=0) dEl.innerHTML='<span class="d-over">'+T().activeLong+'</span>';
  else dEl.innerHTML='<span class="'+(t.estDaysLeft<=5?'d-warn':'d-ok')+'">'+t.estDaysLeft+'d</span>';
}

/* 移动端：列表 ↔ 看盘 单视图切换 */
function showChart(){ document.body.classList.add('m-chart'); resizeCharts(); }
function showList(){ document.body.classList.remove('m-chart'); }
function resizeCharts(){
  requestAnimationFrame(()=>{
    const pe=document.getElementById('price'), me=document.getElementById('macd');
    if(priceChart && pe) priceChart.applyOptions({width:pe.clientWidth||320, height:pe.clientHeight||200});
    if(macdChart && me)  macdChart.applyOptions({width:me.clientWidth||320, height:me.clientHeight||120});
  });
}

async function selectToken(t, fromUser){
  S.selected=t;
  document.getElementById('ch-flag').style.display='none';
  renderTable(); updateHeader();
  if(fromUser && isMobile()) showChart();
  await loadChart(true);
}

/* 简单移动平均(SMA)，与币安 K 线 MA 一致；不足周期返回 null(不画) */
function sma(vals, period){
  const out=new Array(vals.length).fill(null);
  let sum=0;
  for(let i=0;i<vals.length;i++){
    sum+=vals[i];
    if(i>=period) sum-=vals[i-period];
    if(i>=period-1) out[i]=sum/period;
  }
  return out;
}

function setMALegend(v){
  setText('ma7-val', fmtVal(v&&v.m7)); setText('ma25-val', fmtVal(v&&v.m25)); setText('ma99-val', fmtVal(v&&v.m99));
}
function setMacdLegend(v){
  setText('dif-val', fmtVal(v&&v.dif)); setText('dea-val', fmtVal(v&&v.dea)); setText('hist-val', fmtVal(v&&v.hist));
}

function initCharts(){
  const opts = el => ({
    width:el.clientWidth||320, height:el.clientHeight||200,
    layout:{background:{type:'solid',color:'transparent'}, textColor:'#8a909a', fontFamily:"'IBM Plex Mono', monospace"},
    grid:{vertLines:{color:'rgba(255,255,255,.035)'}, horzLines:{color:'rgba(255,255,255,.035)'}},
    timeScale:{timeVisible:true, secondsVisible:false, borderColor:'#23272e'},
    rightPriceScale:{borderColor:'#23272e'},
    crosshair:{mode:LightweightCharts.CrosshairMode.Normal,
      vertLine:{color:'#4a4f58',labelBackgroundColor:'#e8b339'},
      horzLine:{color:'#4a4f58',labelBackgroundColor:'#e8b339'}},
    handleScale:{axisPressedMouseMove:{price:false}},   // 价格轴禁手动拖动缩放，始终自动适配（避免被拖死后“看不到K线”）
  });
  const pe=document.getElementById('price');
  priceChart=LightweightCharts.createChart(pe, opts(pe));
  candleS=priceChart.addCandlestickSeries({
    upColor:C_UP, downColor:C_DOWN, borderUpColor:C_UP, borderDownColor:C_DOWN,
    wickUpColor:C_UP, wickDownColor:C_DOWN});
  // MA 均线(覆盖在蜡烛上)：MA7 橙 / MA25 紫 / MA99 蓝
  const maOpt = color => ({color, lineWidth:1, priceLineVisible:false,
    lastValueVisible:false, crosshairMarkerVisible:false});
  ma7S =priceChart.addLineSeries(maOpt('#ff9900'));
  ma25S=priceChart.addLineSeries(maOpt('#cc44ff'));
  ma99S=priceChart.addLineSeries(maOpt('#00aaff'));

  const me=document.getElementById('macd');
  macdChart=LightweightCharts.createChart(me, opts(me));
  // 高级别 MACD 叠加：单独挂一条隐藏 price scale('htf')，与本级别 MACD 互不抢量纲。
  // 先建(画在底层) → 本级别 MACD 后建、覆盖其上。浅色柱按正负上色(绿/红)。
  htfHistS=macdChart.addHistogramSeries({base:0, priceScaleId:'htf', priceLineVisible:false, lastValueVisible:false});
  htfDifS =macdChart.addLineSeries({priceScaleId:'htf', color:'rgba(38,198,218,.5)', lineWidth:1, priceLineVisible:false, lastValueVisible:false, crosshairMarkerVisible:false});
  htfDeaS =macdChart.addLineSeries({priceScaleId:'htf', color:'rgba(255,167,38,.5)', lineWidth:1, priceLineVisible:false, lastValueVisible:false, crosshairMarkerVisible:false});
  macdChart.priceScale('htf').applyOptions({scaleMargins:{top:0.08, bottom:0.08}, visible:false});
  histS=macdChart.addHistogramSeries({base:0, priceLineVisible:false, lastValueVisible:false});
  difS =macdChart.addLineSeries({color:'#26c6da', lineWidth:1, priceLineVisible:false, lastValueVisible:false, crosshairMarkerVisible:false});
  deaS =macdChart.addLineSeries({color:'#ffa726', lineWidth:1, priceLineVisible:false, lastValueVisible:false, crosshairMarkerVisible:false});

  // 两个面板时间轴联动（防重入）
  let syncing=false;
  const link=(a,b)=>a.timeScale().subscribeVisibleLogicalRangeChange(r=>{
    if(syncing||!r) return; syncing=true;
    try{ b.timeScale().setVisibleLogicalRange(r); }catch(e){}
    syncing=false;
  });
  link(priceChart, macdChart); link(macdChart, priceChart);

  // 十字线移动 → 图例显示该位置数值（手机划动可读数；离开则显示最新值）
  priceChart.subscribeCrosshairMove(p=>{
    if(!p || !p.time || !p.seriesData){ setMALegend(S.lastMA); return; }
    const g=s=>{const d=p.seriesData.get(s); return d?d.value:null;};
    setMALegend({m7:g(ma7S), m25:g(ma25S), m99:g(ma99S)});
  });
  macdChart.subscribeCrosshairMove(p=>{
    if(!p || !p.time || !p.seriesData){ setMacdLegend(S.lastMACD); return; }
    const g=s=>{const d=p.seriesData.get(s); return d?d.value:null;};
    setMacdLegend({dif:g(difS), dea:g(deaS), hist:g(histS)});
  });

  new ResizeObserver(()=>priceChart.applyOptions({width:pe.clientWidth||320, height:pe.clientHeight||200})).observe(pe);
  new ResizeObserver(()=>macdChart.applyOptions({width:me.clientWidth||320, height:me.clientHeight||120})).observe(me);
}

function buildMarkers(divs){
  return divs.map(d=>{
    const bull=d.kind==='bullish';
    let txt=d.missed?'L0':('L'+d.level);
    if(d.double) txt+='+';
    if(d.provisional) txt+='?';
    return {
      time:d.time,
      position:bull?'belowBar':'aboveBar',
      shape:bull?'arrowUp':'arrowDown',
      color:d.provisional?C_PROV:(bull?C_BULL:C_BEAR),
      text:txt,
    };
  });
}

/* 高级别背离箭头：琥珀色 + 周期前缀(如 "5m L2")，与本级别红绿箭头区分 */
function buildHtfMarkers(divs, iv){
  const pre = iv ? iv+' ' : '';
  return divs.map(d=>{
    const bull=d.kind==='bullish';
    let txt=pre+(d.missed?'L0':('L'+d.level));
    if(d.double) txt+='+';
    if(d.provisional) txt+='?';
    return {
      time:d.time,
      position:bull?'belowBar':'aboveBar',
      shape:bull?'arrowUp':'arrowDown',
      color:d.provisional?C_HTF_DIM:C_HTF,
      text:txt,
    };
  });
}

/* 高级别 MACD 各根的 hist 值：time -> hist（无未来函数投影值，与屏幕一致） */
function htfHistMap(){
  const m=new Map();
  (S.htfMacd||[]).forEach(x=>{ if('hist' in x) m.set(x.time, x.hist); });
  return m;
}

/* 顺势过滤：高级别叠加开启 + 开关打开时，逐根隐藏与高级别 MACD 方向相反的本级别信号。
   高级别向上(hist>0) → 删本级别卖出(顶背离)；向下(hist<0) → 删本级别买入(底背离)。
   高级别该处无方向(hist=0 / 缺失 / 数据不足) → 保留。逐根判断，故卖出信号只在其下方
   高级别柱为红时才会留下，与屏幕一致。 */
function filteredBaseMarkers(){
  if(!S.trendFilter || !S.htfInterval) return S.markers;
  const hm=htfHistMap();
  return S.markers.filter(d=>{
    const h=hm.get(d.time);
    if(h===undefined || h===0) return true;
    if(h>0 && d.kind!=='bullish') return false;     // 大级别向上：删卖出(顶背离)
    if(h<0 && d.kind==='bullish') return false;     // 大级别向下：删买入(底背离)
    return true;
  });
}

/* 本级别(过滤后) + 高级别标记合并，喂给 setMarkers，并更新信号计数 */
function applyMarkers(){
  const base=filteredBaseMarkers();
  const all=S.showMarkers
    ? [...buildMarkers(base), ...buildHtfMarkers(S.htfMarkers, S.htfInterval)].sort((a,b)=>a.time-b.time)
    : [];
  if(candleS) candleS.setMarkers(all);
  setText('sig-count', base.length + S.htfMarkers.length);
}

function toggleMarkers(){
  S.showMarkers=document.getElementById('mk-toggle').checked;
  applyMarkers();
}

function onTrendFilterChange(){
  S.trendFilter=document.getElementById('tf-toggle').checked;
  applyMarkers();
}

async function loadChart(fit){
  if(!S.selected) return;
  const sym=S.selected.pair;                       // 完整交易对(BTCUSDT / ALPHA_162USDT)
  const market=S.selected.market||'alpha';         // 锁定市场：自选→现货 / Alpha→盘口
  try{
    const lim=BASE_LIMITS[S.interval]||500;
    const r=await fetch(`/api/klines?symbol=${encodeURIComponent(sym)}&interval=${S.interval}&limit=${lim}&htf=${S.htf}&market=${market}`);
    const j=await r.json();
    if(!j.ok){ showErr(j.error); return; }
    showErr(null);
    const c=j.candles;
    const prec=precOf(S.selected.price||(c.length?c[c.length-1].close:1));
    candleS.applyOptions({priceFormat:{type:'price', precision:prec, minMove:Math.pow(10,-prec)}});
    candleS.setData(c.map(k=>({time:k.time, open:k.open, high:k.high, low:k.low, close:k.close})));
    candleS.priceScale().applyOptions({autoScale:true});   // 每次恢复自动缩放，避免价格轴被卡死导致“看不到K线”

    // MA 均线
    const closes=c.map(k=>k.close);
    const m7=sma(closes,7), m25=sma(closes,25), m99=sma(closes,99);
    const maData=arr=>c.map((k,i)=>({time:k.time, value:arr[i]})).filter(x=>x.value!=null);
    ma7S.setData(maData(m7)); ma25S.setData(maData(m25)); ma99S.setData(maData(m99));
    const li=closes.length-1;
    S.lastMA = li>=0 ? {m7:m7[li], m25:m25[li], m99:m99[li]} : null;
    setMALegend(S.lastMA);

    // MACD 面板（无 MACD 数据时用 K 线时间填 whitespace，保证两面板 bar 数一致、联动不错位）
    const macd=(j.macd&&j.macd.length)?j.macd:c.map(k=>({time:k.time}));
    histS.setData(macd.map(m=>('hist' in m)
      ? {time:m.time, value:m.hist, color:m.hist>=0?'rgba(38,166,154,.6)':'rgba(239,83,80,.6)'}
      : {time:m.time}));
    difS.setData(macd.map(m=>('dif' in m)?{time:m.time, value:m.dif}:{time:m.time}));
    deaS.setData(macd.map(m=>('dea' in m)?{time:m.time, value:m.dea}:{time:m.time}));
    let lm=null;
    for(let i=macd.length-1;i>=0;i--){ if('hist' in macd[i]){ lm={dif:macd[i].dif, dea:macd[i].dea, hist:macd[i].hist}; break; } }
    S.lastMACD=lm; setMacdLegend(lm);

    // 背离标记(本级别)
    S.markers=j.divergences||[];

    // 高级别 MACD 叠加 + 高级别背离箭头
    const htf=j.htf;
    const htfLeg=document.getElementById('htf-leg');
    if(htf && htf.available && htf.macd && htf.macd.length){
      S.htfInterval=htf.interval;
      htfHistS.setData(htf.macd.map(m=>('hist' in m)
        ? {time:m.time, value:m.hist, color:m.hist>=0?'rgba(38,166,154,.28)':'rgba(239,83,80,.28)'}
        : {time:m.time}));
      htfDifS.setData(htf.macd.map(m=>('dif' in m)?{time:m.time, value:m.dif}:{time:m.time}));
      htfDeaS.setData(htf.macd.map(m=>('dea' in m)?{time:m.time, value:m.dea}:{time:m.time}));
      S.htfMarkers=htf.markers||[]; S.htfMacd=htf.macd;
      if(htfLeg){ setText('htf-leg-iv', htf.interval); htfLeg.style.display='flex'; }
    }else{
      S.htfInterval=null; S.htfMarkers=[]; S.htfMacd=[];
      if(htfHistS) htfHistS.setData([]);
      if(htfDifS)  htfDifS.setData([]);
      if(htfDeaS)  htfDeaS.setData([]);
      if(htfLeg){
        if(htf && htf.interval){ setText('htf-leg-iv', htf.interval+' · '+T().htfNA); htfLeg.style.display='flex'; }
        else htfLeg.style.display='none';
      }
    }

    applyMarkers();

    if(fit){
      const n=c.length, r={from:Math.max(0,n-140), to:n};   // 聚焦最近 ~140 根（像币安），旧数据可左滑查看
      priceChart.timeScale().setVisibleLogicalRange(r);
      macdChart.timeScale().setVisibleLogicalRange(r);
    }
  }catch(e){ showErr(''+e); }
}

function renderIntervals(){
  const box=document.getElementById('intervals');
  box.innerHTML=INTERVALS.map(iv=>`<button class="iv${iv===S.interval?' on':''}" data-iv="${iv}">${iv}</button>`).join('');
  [...box.querySelectorAll('.iv')].forEach(b=>{
    b.onclick=()=>{ S.interval=b.dataset.iv; renderIntervals(); loadChart(true); };
  });
  renderHtfSelect();
}

/* 高级别参考周期下拉：选项 = 比当前更高的周期 + 自动(默认梯子) + 关 */
function renderHtfSelect(){
  const sel=document.getElementById('htf-select');
  if(!sel) return;
  const t=T();
  const higher=INTERVALS.slice(INTERVALS.indexOf(S.interval)+1);
  const auto=higher[0]||null;                       // 默认梯子 = 紧邻上一档
  // 切换本级别后，原选择可能失效 → 回落到自动
  if(S.htf!=='auto' && S.htf!=='off' && !higher.includes(S.htf)) S.htf='auto';
  let opts=`<option value="auto">${t.htfAuto}${auto?(' ('+auto+')'):''}</option>`
          +`<option value="off">${t.htfOff}</option>`;
  higher.forEach(iv=>{ opts+=`<option value="${iv}">${iv}</option>`; });
  sel.innerHTML=opts;
  if(higher.length===0){ sel.value='off'; sel.disabled=true; }   // 1d 之上无更高，置灰
  else { sel.value=S.htf; sel.disabled=false; }
}

function onHtfChange(){
  S.htf=document.getElementById('htf-select').value;
  loadChart(false);
}

/* ===== 列表加载 / 标签页切换 ===== */

// Alpha：4倍代币(盘口成交额排序)。notX4 警示仅对 Alpha 生效。
async function loadAlpha(){
  try{
    const r=await fetch('/api/tokens'); const j=await r.json();
    if(!j.ok){ if(S.tab==='alpha') showErr(j.error); return; }
    S.alphaTokens=j.tokens;
    syncSelectedFrom(S.alphaTokens, true);
    if(S.tab==='alpha'){
      showErr(null);
      setText('refresh-time', new Date().toLocaleTimeString());
      renderTable(); maybeAutoSelect();
    }
  }catch(e){ if(S.tab==='alpha') showErr(''+e); }
}

// 自选：主流现货 24h 行情。失败不影响 Alpha 标签。
async function loadWatch(){
  try{
    const r=await fetch('/api/watchlist'); const j=await r.json();
    if(!j.ok){ if(S.tab==='watch') showErr(j.error); return; }
    S.watchTokens=j.tokens;
    syncSelectedFrom(S.watchTokens, false);
    if(S.tab==='watch'){
      showErr(null);
      setText('refresh-time', new Date().toLocaleTimeString());
      renderTable(); maybeAutoSelect();
    }
  }catch(e){ if(S.tab==='watch') showErr(''+e); }
}

// 当前标签页对应的数据加载
function loadActive(){ return S.tab==='alpha' ? loadAlpha() : loadWatch(); }
// 两个列表都刷新(定时器用，保证后台标签数据也新鲜)
function refreshAll(){ loadAlpha(); loadWatch(); }

// 把 S.selected 同步到最新列表数据(仅当选中项属于该列表)。isAlpha=true 时维护 notX4 警示。
function syncSelectedFrom(list, isAlpha){
  if(!S.selected) return;
  const selIsAlpha = (S.selected.market||'alpha')==='alpha';
  if(selIsAlpha!==isAlpha) return;                 // 选中项不属于这个列表，跳过
  const fresh=list.find(t=>t.pair===S.selected.pair);
  const flag=document.getElementById('ch-flag');
  if(fresh){
    S.selected=fresh; updateHeader();
    if(isAlpha && flag) flag.style.display='none';
  }else if(isAlpha && flag){                        // 仅 Alpha：选中币掉出 4 倍列表 → 警示
    flag.textContent=T().notX4; flag.style.display='inline';
  }
}

// 当前列表有数据但尚未选中任何币 → 自动选第一个
function maybeAutoSelect(){
  const list=currentList();
  if(!S.selected && list.length){ selectToken(list[0], false); }
}

/* ===== 标签页 UI（自选 / Alpha） ===== */
function renderTabbar(){
  const bar=document.getElementById('tabbar'); if(!bar) return;
  const tabs=[['watch',T().tabWatch],['alpha',T().tabAlpha]];
  bar.innerHTML=tabs.map(([k,label])=>
    `<button class="tab${S.tab===k?' on':''}" data-tab="${k}">${label}</button>`).join('');
  [...bar.querySelectorAll('.tab')].forEach(el=>{ el.onclick=()=>switchTab(el.dataset.tab); });
}

// 表头 + 计数标签随当前页切换
function updateTabUI(){
  const t=T();
  setText('left-h', S.tab==='alpha' ? t.leftHAlpha : t.leftHWatch);
  setText('meta-tok', S.tab==='alpha' ? t.metaTok : t.tabWatch);
  setText('count', currentList().length);
}

function switchTab(tab){
  if(tab===S.tab || (tab!=='alpha' && tab!=='watch')) return;
  S.tab=tab;
  renderTabbar(); updateTabUI();
  showErr(null);
  renderTable();                                    // 已有缓存先渲染，不闪烁
  loadActive();                                     // 再拉该页最新数据
  maybeAutoSelect();
}

// boot
try{ const sv=localStorage.getItem('alpha4x_lang'); if(sv==='zh'||sv==='en') S.lang=sv; }catch(e){}
initCharts();
renderIntervals();
applyLang();
loadActive();                              // 先拉当前(默认自选)标签数据并自动选中
loadAlpha();                               // 预拉 Alpha 列表，切到该标签即时可见
setInterval(refreshAll, 60000);            // 两个列表每 60s 刷新
setInterval(()=>loadChart(false), 30000); // 选中的图每 30s 刷新(不重置缩放)
window.addEventListener('orientationchange', ()=>{ if(document.body.classList.contains('m-chart')) resizeCharts(); });
</script>
</body>
</html>"""


if __name__ == "__main__":
    import sys, socket

    # 端口：命令行第一个参数指定，不写则默认 5000
    port = 5000
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
            if not (1 <= port <= 65535):
                raise ValueError
        except ValueError:
            print(f"端口无效：{sys.argv[1]}（应为 1-65535 的整数，建议用 >1024 的，如 8080/8000/5050）")
            sys.exit(1)

    # 自动探测本机局域网 IP
    try:
        _s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        _s.connect(("8.8.8.8", 80)); lan_ip = _s.getsockname()[0]; _s.close()
    except Exception:
        lan_ip = "<你的局域网IP>"

    if not DIVERGENCE_OK:
        print(f"[提示] 未能加载背离模块（{_DIV_ERR}）—— 终端会照常显示 K 线，但没有 MACD / 背离。")
        print("       请确认 indicator.py / divergence.py 与本文件同目录，并 pip install pandas numpy。")
    print("uniark-trade 短线王 已启动：")
    print(f"  本机访问    ->  http://127.0.0.1:{port}")
    print(f"  局域网访问  ->  http://{lan_ip}:{port}   (手机/其他电脑用这个)")
    # host=0.0.0.0 监听所有网卡（局域网可访问）。只读无密钥，家用网络无妨；勿映射公网。
    app.run(host="0.0.0.0", port=port, debug=False)
