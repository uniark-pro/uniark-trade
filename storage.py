"""
storage.py — uniark-trade 看板的本地 K 线缓存层（SQLite）
========================================================

只做一件事：把拉回来的 K 线**落本地库**，供看板秒开。

- 网页端 /api/klines 先读这里（命中即零网络往返），未命中/过期才回退实时拉一次并写库。
- 后台采集线程把自选现货常驻刷进这里，所以默认页永远是热的。
- **本模块不存任何指标**：MACD / 背离 / 高级别投影仍在缓存到的 K 线上现算
  （瓶颈一直是网络，不是算力；几百根上现算只要毫秒级），故 indicator.py /
  divergence.py / htf_overlay.py 一行不用动。

设计要点
--------
- 单表 `klines`，复合主键 (market, symbol, interval, open_time) → INSERT OR REPLACE
  天然去重幂等。
- 复合索引完全对齐查询模式 (market, symbol, interval, open_time DESC)，查询是纯索引
  区间扫描，不全表扫、不额外排序。
- WAL 日志模式 + synchronous=NORMAL：采集线程写的同时，网页请求线程能并发读，互不阻塞。
- 每线程一条连接（thread-local）：sqlite3 连接不跨线程共享；WAL 允许多读 + 单写。

K 线 dict 约定（与 uniark-trade.py 的 fetch_klines 输出一致）
-----------------------------------------------------------
    {time(秒), open, high, low, close, volume, quoteVol, trades}

公开接口
--------
- init_db()                              建表/建索引（幂等）
- upsert(market, symbol, interval, rows) 批量写入（去重）
- query(market, symbol, interval, limit) 取最近 limit 根（时间正序）
- latest_open_time(market, symbol, iv)   最新一根开盘时间（秒），无则 None
- stats()                                每条 (market|symbol|interval) 的条数与最新时间
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

# 缓存库路径（与本文件同目录下的 data/）。可按需改。
DB_PATH = Path(__file__).parent / "data" / "kline_cache.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS klines (
    market     TEXT    NOT NULL,          -- 'spot' | 'alpha'（隔离两市场，避免符号撞车）
    symbol     TEXT    NOT NULL,          -- 完整交易对：BTCUSDT / ALPHA_162USDT
    interval   TEXT    NOT NULL,          -- 1m / 5m / 15m / 1h / 4h / 1d
    open_time  INTEGER NOT NULL,          -- 开盘时间（秒）
    open       REAL    NOT NULL,
    high       REAL    NOT NULL,
    low        REAL    NOT NULL,
    close      REAL    NOT NULL,
    volume     REAL    NOT NULL,
    quote_vol  REAL    NOT NULL,          -- USDT 成交额
    trades     INTEGER NOT NULL,
    PRIMARY KEY (market, symbol, interval, open_time)
);
CREATE INDEX IF NOT EXISTS idx_klines_lookup
    ON klines (market, symbol, interval, open_time DESC);
"""

_local = threading.local()


def _conn() -> sqlite3.Connection:
    """每线程一条连接（WAL 下可多读单写）。首次访问时建连 + 打开 WAL。"""
    c = getattr(_local, "conn", None)
    if c is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(str(DB_PATH), isolation_level=None, check_same_thread=False)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        _local.conn = c
    return c


def init_db() -> None:
    """建表 + 建索引（幂等，可重复调用）。"""
    _conn().executescript(_SCHEMA)


def upsert(market: str, symbol: str, interval: str, rows: list[dict]) -> int:
    """批量写入 K 线；按主键去重（INSERT OR REPLACE）。返回写入条数。"""
    if not rows:
        return 0
    sql = (
        "INSERT OR REPLACE INTO klines "
        "(market, symbol, interval, open_time, open, high, low, close, volume, quote_vol, trades) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )
    payload = [
        (
            market, symbol, interval, int(r["time"]),
            float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]),
            float(r.get("volume", 0.0)), float(r.get("quoteVol", 0.0)), int(r.get("trades", 0)),
        )
        for r in rows
    ]
    # 必须 executemany —— execute 不会按行迭代 list of tuples
    cur = _conn().executemany(sql, payload)
    return cur.rowcount


def query(market: str, symbol: str, interval: str, limit: int = 500) -> list[dict]:
    """取最近 limit 根（DB 内按 open_time DESC 取，再翻转成时间正序返回）。"""
    sql = (
        "SELECT open_time, open, high, low, close, volume, quote_vol, trades "
        "FROM klines WHERE market = ? AND symbol = ? AND interval = ? "
        "ORDER BY open_time DESC LIMIT ?"
    )
    rows = _conn().execute(sql, (market, symbol, interval, limit)).fetchall()
    rows.reverse()
    return [
        {
            "time":     r[0],
            "open":     r[1],
            "high":     r[2],
            "low":      r[3],
            "close":    r[4],
            "volume":   r[5],
            "quoteVol": r[6],
            "trades":   r[7],
        }
        for r in rows
    ]


def latest_open_time(market: str, symbol: str, interval: str):
    """最新一根开盘时间（秒）；该流尚无数据则 None。"""
    r = _conn().execute(
        "SELECT MAX(open_time) FROM klines WHERE market = ? AND symbol = ? AND interval = ?",
        (market, symbol, interval),
    ).fetchone()
    return r[0] if r and r[0] is not None else None


def stats() -> dict:
    """每条 (market|symbol|interval) 的条数与最新时间——调试/监控用。"""
    rows = _conn().execute(
        "SELECT market, symbol, interval, COUNT(*), MAX(open_time) "
        "FROM klines GROUP BY market, symbol, interval"
    ).fetchall()
    return {
        f"{m}|{s}|{iv}": {"market": m, "symbol": s, "interval": iv, "count": n, "last_open": t}
        for m, s, iv, n, t in rows
    }
