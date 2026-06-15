"""
技术指标定义：EMA、MACD
"""
import pandas as pd


def calc_ema(series, period):
    return series.ewm(span=period, adjust=False).mean()


def calc_macd(close, fast=12, slow=26, signal=9):
    ema_fast = calc_ema(close, fast)
    ema_slow = calc_ema(close, slow)
    macd_line = ema_fast - ema_slow
    signal_line = calc_ema(macd_line, signal)
    macd_hist = macd_line - signal_line
    return macd_line, signal_line, macd_hist


def add_indicators(df):
    macd_dif, macd_dea, macd_hist = calc_macd(df['close'])
    df['macd'] = macd_dif
    df['signal'] = macd_dea
    df['hist'] = macd_hist
    return df
