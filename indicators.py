"""
기술적 지표 모듈

- RSI / ATR / EMA / 볼륨 필터
- RSI 다이버전스 감지
  · 일반 강세 다이버전스 (Regular Bullish): 가격 저점↓ + RSI 저점↑ → 상승 반전
  · 일반 약세 다이버전스 (Regular Bearish): 가격 고점↑ + RSI 고점↓ → 하락 반전
  · 히든 강세 다이버전스 (Hidden Bullish):  가격 저점↑ + RSI 저점↓ → 상승 추세 지속
  · 히든 약세 다이버전스 (Hidden Bearish):  가격 고점↓ + RSI 고점↑ → 하락 추세 지속
"""

import numpy as np
import pandas as pd
from dataclasses import dataclass
from typing import Optional
import logging

logger = logging.getLogger(__name__)

RSI_PERIOD = 14
RSI_OVERBOUGHT = 70
RSI_OVERSOLD = 30
DIVERGENCE_LOOKBACK = 50
PIVOT_WINDOW = 5
ATR_PERIOD = 14
VOLUME_MA_PERIOD = 20


@dataclass
class DivergenceResult:
    detected: bool = False
    div_type: str = "none"          # "regular_bullish" | "regular_bearish" | "hidden_bullish" | "hidden_bearish"
    description: str = ""           # 한글 설명
    strength: str = "none"          # "strong" | "moderate" | "weak"
    rsi_value: float = 0.0          # 현재 RSI 값
    prev_price_pivot: float = 0.0   # 비교 기준 이전 가격 피벗
    prev_rsi_pivot: float = 0.0     # 비교 기준 이전 RSI 피벗
    signal_direction: str = "none"  # "long" | "short" | "none"


def calculate_rsi(df: pd.DataFrame, period: int = RSI_PERIOD) -> pd.Series:
    """RSI 계산"""
    close = df["close"]
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi.fillna(50)


def _find_price_pivots(series: pd.Series, window: int = PIVOT_WINDOW) -> list[tuple[int, float]]:
    """
    가격/RSI 시리즈에서 피벗 고점/저점 인덱스와 값 반환
    반환: [(index, value), ...]
    """
    pivots_high = []
    pivots_low = []
    arr = series.values

    for i in range(window, len(arr) - window):
        if all(arr[i] >= arr[i - j] for j in range(1, window + 1)) and \
           all(arr[i] >= arr[i + j] for j in range(1, window + 1)):
            pivots_high.append((i, arr[i]))
        elif all(arr[i] <= arr[i - j] for j in range(1, window + 1)) and \
             all(arr[i] <= arr[i + j] for j in range(1, window + 1)):
            pivots_low.append((i, arr[i]))

    return pivots_high, pivots_low


def detect_rsi_divergence(df: pd.DataFrame, period: int = RSI_PERIOD) -> DivergenceResult:
    """
    RSI 다이버전스 감지 (최근 DIVERGENCE_LOOKBACK 캔들 기준)

    Args:
        df: OHLCV DataFrame
        period: RSI 기간

    Returns:
        DivergenceResult
    """
    result = DivergenceResult()

    if len(df) < period + DIVERGENCE_LOOKBACK:
        return result

    # 분석 범위 슬라이싱
    recent = df.iloc[-DIVERGENCE_LOOKBACK:].copy()
    rsi = calculate_rsi(df, period).iloc[-DIVERGENCE_LOOKBACK:]
    recent["rsi"] = rsi.values

    current_rsi = float(recent["rsi"].iloc[-1])
    result.rsi_value = round(current_rsi, 2)

    price_highs, price_lows = _find_price_pivots(recent["close"], PIVOT_WINDOW)
    rsi_highs, rsi_lows = _find_price_pivots(recent["rsi"], PIVOT_WINDOW)

    # ─── 강세 다이버전스 (저점 비교) ─────────────────────────────
    if len(price_lows) >= 2 and len(rsi_lows) >= 2:
        # 가장 최근 두 저점 비교
        (pi1, pv1), (pi2, pv2) = price_lows[-2], price_lows[-1]
        # RSI에서 같은 구간의 저점 찾기
        rsi_low_candidates = [(ri, rv) for ri, rv in rsi_lows if ri >= pi1 - 3]
        if len(rsi_low_candidates) >= 2:
            (ri1, rv1), (ri2, rv2) = rsi_low_candidates[-2], rsi_low_candidates[-1]

            # 일반 강세 다이버전스: 가격↓ + RSI↑
            if pv2 < pv1 and rv2 > rv1:
                strength = _get_strength(abs(pv2 - pv1) / pv1, abs(rv2 - rv1))
                result.detected = True
                result.div_type = "regular_bullish"
                result.signal_direction = "long"
                result.prev_price_pivot = round(pv1, 4)
                result.prev_rsi_pivot = round(rv1, 2)
                result.strength = strength
                result.description = (
                    f"일반 강세 다이버전스 (Regular Bullish)\n"
                    f"    가격: {pv1:.4f} → {pv2:.4f} (저점 하락)\n"
                    f"    RSI : {rv1:.1f} → {rv2:.1f} (저점 상승)\n"
                    f"    → 매도 압력 약화, 상승 반전 가능성"
                )
                return result

            # 히든 강세 다이버전스: 가격↑ + RSI↓
            if pv2 > pv1 and rv2 < rv1:
                strength = _get_strength(abs(pv2 - pv1) / pv1, abs(rv2 - rv1))
                result.detected = True
                result.div_type = "hidden_bullish"
                result.signal_direction = "long"
                result.prev_price_pivot = round(pv1, 4)
                result.prev_rsi_pivot = round(rv1, 2)
                result.strength = strength
                result.description = (
                    f"히든 강세 다이버전스 (Hidden Bullish)\n"
                    f"    가격: {pv1:.4f} → {pv2:.4f} (저점 상승)\n"
                    f"    RSI : {rv1:.1f} → {rv2:.1f} (저점 하락)\n"
                    f"    → 상승 추세 지속 확인"
                )
                return result

    # ─── 약세 다이버전스 (고점 비교) ─────────────────────────────
    if len(price_highs) >= 2 and len(rsi_highs) >= 2:
        (pi1, pv1), (pi2, pv2) = price_highs[-2], price_highs[-1]
        rsi_high_candidates = [(ri, rv) for ri, rv in rsi_highs if ri >= pi1 - 3]
        if len(rsi_high_candidates) >= 2:
            (ri1, rv1), (ri2, rv2) = rsi_high_candidates[-2], rsi_high_candidates[-1]

            # 일반 약세 다이버전스: 가격↑ + RSI↓
            if pv2 > pv1 and rv2 < rv1:
                strength = _get_strength(abs(pv2 - pv1) / pv1, abs(rv2 - rv1))
                result.detected = True
                result.div_type = "regular_bearish"
                result.signal_direction = "short"
                result.prev_price_pivot = round(pv1, 4)
                result.prev_rsi_pivot = round(rv1, 2)
                result.strength = strength
                result.description = (
                    f"일반 약세 다이버전스 (Regular Bearish)\n"
                    f"    가격: {pv1:.4f} → {pv2:.4f} (고점 상승)\n"
                    f"    RSI : {rv1:.1f} → {rv2:.1f} (고점 하락)\n"
                    f"    → 매수 압력 약화, 하락 반전 가능성"
                )
                return result

            # 히든 약세 다이버전스: 가격↓ + RSI↑
            if pv2 < pv1 and rv2 > rv1:
                strength = _get_strength(abs(pv2 - pv1) / pv1, abs(rv2 - rv1))
                result.detected = True
                result.div_type = "hidden_bearish"
                result.signal_direction = "short"
                result.prev_price_pivot = round(pv1, 4)
                result.prev_rsi_pivot = round(rv1, 2)
                result.strength = strength
                result.description = (
                    f"히든 약세 다이버전스 (Hidden Bearish)\n"
                    f"    가격: {pv1:.4f} → {pv2:.4f} (고점 하락)\n"
                    f"    RSI : {rv1:.1f} → {rv2:.1f} (고점 상승)\n"
                    f"    → 하락 추세 지속 확인"
                )
                return result

    return result


def _get_strength(price_diff_pct: float, rsi_diff: float) -> str:
    """다이버전스 강도 평가"""
    score = 0
    if price_diff_pct > 0.03:
        score += 1
    if rsi_diff > 10:
        score += 1
    if score == 2:
        return "strong"
    elif score == 1:
        return "moderate"
    return "weak"


def get_rsi_zone(rsi_value: float) -> str:
    """RSI 구간 판단"""
    if rsi_value >= RSI_OVERBOUGHT:
        return f"과매수 ({rsi_value:.1f})"
    elif rsi_value <= RSI_OVERSOLD:
        return f"과매도 ({rsi_value:.1f})"
    elif rsi_value >= 60:
        return f"강세 ({rsi_value:.1f})"
    elif rsi_value <= 40:
        return f"약세 ({rsi_value:.1f})"
    else:
        return f"중립 ({rsi_value:.1f})"


def divergence_aligns_with_signal(div: DivergenceResult, wave_direction: str) -> tuple[bool, str]:
    """
    다이버전스 방향이 파동 신호 방향과 일치하는지 확인

    Returns:
        (일치 여부, 설명 문자열)
    """
    if not div.detected:
        return False, "다이버전스 미감지"

    wave_side = "long" if wave_direction == "up" else "short"

    if div.signal_direction == wave_side:
        return True, f"파동 방향과 일치 ({div.strength} 강도)"
    else:
        return False, f"파동 방향과 불일치 (파동:{wave_side} ↔ 다이버전스:{div.signal_direction})"


# ══════════════════════════════════════════════════════════════
# ATR (Average True Range) — 동적 SL/TP 산정용
# ══════════════════════════════════════════════════════════════

def calculate_atr(df: pd.DataFrame, period: int = ATR_PERIOD) -> pd.Series:
    """ATR 계산 (Wilder 방식)"""
    high = df["high"]
    low = df["low"]
    close = df["close"]
    prev_close = close.shift(1)

    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)

    return tr.ewm(alpha=1.0 / period, min_periods=period).mean()


def get_atr_stop_distance(df: pd.DataFrame, multiplier: float = 2.0,
                          period: int = ATR_PERIOD) -> float:
    """현재 ATR 기반 손절 거리(가격 단위) 반환"""
    atr = calculate_atr(df, period)
    current_atr = float(atr.iloc[-1])
    return current_atr * multiplier


def get_atr_pct(df: pd.DataFrame, period: int = ATR_PERIOD) -> float:
    """현재가 대비 ATR 비율 (%) — 변동성 지표"""
    atr = calculate_atr(df, period)
    current_price = float(df["close"].iloc[-1])
    if current_price <= 0:
        return 0.0
    return float(atr.iloc[-1]) / current_price


# ══════════════════════════════════════════════════════════════
# EMA — 추세 확인용
# ══════════════════════════════════════════════════════════════

def calculate_ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def get_ema_trend(df: pd.DataFrame, fast: int = 9, slow: int = 21) -> str:
    """EMA 기반 추세 방향: 'up' / 'down' / 'sideways'"""
    close = df["close"]
    ema_f = calculate_ema(close, fast)
    ema_s = calculate_ema(close, slow)
    current_price = float(close.iloc[-1])

    above_fast = current_price > float(ema_f.iloc[-1])
    above_slow = current_price > float(ema_s.iloc[-1])
    fast_above_slow = float(ema_f.iloc[-1]) > float(ema_s.iloc[-1])

    if above_fast and above_slow and fast_above_slow:
        return "up"
    elif not above_fast and not above_slow and not fast_above_slow:
        return "down"
    return "sideways"


# ══════════════════════════════════════════════════════════════
# 볼륨 필터 — 가짜 신호 방지
# ══════════════════════════════════════════════════════════════

def is_volume_confirmed(df: pd.DataFrame, lookback: int = VOLUME_MA_PERIOD,
                        threshold: float = 1.0) -> bool:
    """
    최근 3봉 평균 거래량이 N기간 평균 대비 threshold 이상인지 확인.
    단일 봉(형성 중/이상치)에 의존하지 않고 최근 흐름을 본다.
    """
    if len(df) < lookback + 4:
        return True

    vol = df["volume"].iloc[:-1]  # 형성 중 봉 제외
    vol_ma = vol.rolling(lookback).mean()
    recent_avg = float(vol.iloc[-3:].mean())
    long_avg = float(vol_ma.iloc[-1])

    if long_avg <= 0:
        return True
    return recent_avg >= long_avg * threshold
