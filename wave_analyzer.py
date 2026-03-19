"""
글렌 닐리 NEoWave + 지침서 기반 파동 분석 엔진

엔진 1: 프랙탈 계층화 (Pivot 추출)
엔진 2: 패턴 식별 + 절대법칙 필터
엔진 3: 추세선 트리거
엔진 4: PRG (Potential Reversal Zone) - 피보나치 중첩
"""

import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
import logging
from rule_loader import R

logger = logging.getLogger(__name__)

# ─── 피보나치 상수 ───────────────────────────────────────────
FIB_236 = 0.236
FIB_382 = 0.382
FIB_500 = 0.500
FIB_618 = 0.618
FIB_786 = 0.786
FIB_886 = 0.886
FIB_1000 = 1.000
FIB_1272 = 1.272
FIB_1382 = 1.382
FIB_1618 = 1.618
FIB_2618 = 2.618


class WaveType(Enum):
    IMPULSE   = "impulse"
    TERMINAL  = "terminal"    # 웨지 (엔딩 다이아고날)
    ZIGZAG    = "zigzag"
    FLAT      = "flat"
    TRIANGLE  = "triangle"
    COMPLEX   = "complex"
    UNKNOWN   = "unknown"


class WavePosition(Enum):
    WAVE1    = "wave1"
    WAVE2    = "wave2"
    WAVE3    = "wave3"
    WAVE4    = "wave4"
    WAVE5    = "wave5"
    WAVE_A   = "wave_a"
    WAVE_B   = "wave_b"
    WAVE_C   = "wave_c"
    COMPLETE = "complete"
    NONE     = "none"


@dataclass
class Pivot:
    index: int
    price: float
    is_high: bool
    timestamp: pd.Timestamp = None


@dataclass
class PRGZone:
    """Potential Reversal Zone - 피보나치 중첩 반전 구간"""
    price_low: float = 0.0
    price_high: float = 0.0
    confluence_count: int = 0
    fib_levels: list = field(default_factory=list)
    valid: bool = False


@dataclass
class TrendlineResult:
    """2-4 추세선 / B-D 추세선 분석"""
    has_24_line: bool = False
    line_24_price: float = 0.0    # 현재가에서의 2-4 추세선 값
    broken_24: bool = False       # 2-4 추세선 돌파 여부 (임펄스 마감 신호)
    has_bd_line: bool = False
    line_bd_price: float = 0.0
    broken_bd: bool = False       # B-D 추세선 돌파 (삼각형 분출 신호)


@dataclass
class WaveCount:
    wave_type: WaveType         = WaveType.UNKNOWN
    current_position: WavePosition = WavePosition.NONE
    direction: str              = "none"   # "up" | "down"
    pivots: list                = field(default_factory=list)
    fib_valid: bool             = False
    entry_zone: bool            = False
    target_price: float         = 0.0
    invalidation_price: float   = 0.0
    confidence: float           = 0.0
    prg: PRGZone                = field(default_factory=PRGZone)
    trendline: TrendlineResult  = field(default_factory=TrendlineResult)
    abs_law_passed: bool        = False    # 절대법칙 통과 여부
    rejection_reason: str       = ""       # 기각 사유


# ══════════════════════════════════════════════════════════════
# 엔진 1: 피벗 추출
# ══════════════════════════════════════════════════════════════

def detect_pivots(df: pd.DataFrame, left: int = None, right: int = None) -> list[Pivot]:
    if left is None:  left  = R.pivot_left
    if right is None: right = R.pivot_right
    """스윙 하이/로우 피벗 감지"""
    pivots = []
    highs = df["high"].values
    lows  = df["low"].values
    timestamps = df.index

    for i in range(left, len(df) - right):
        is_swing_high = all(highs[i] >= highs[i - j] for j in range(1, left + 1)) and \
                        all(highs[i] >= highs[i + j] for j in range(1, right + 1))
        is_swing_low  = all(lows[i]  <= lows[i - j]  for j in range(1, left + 1)) and \
                        all(lows[i]  <= lows[i + j]   for j in range(1, right + 1))

        if is_swing_high:
            pivots.append(Pivot(i, highs[i], True,  timestamps[i]))
        elif is_swing_low:
            pivots.append(Pivot(i, lows[i],  False, timestamps[i]))

    # 연속 동일 타입 → 더 극단적인 값 유지
    filtered = []
    for p in pivots:
        if filtered and filtered[-1].is_high == p.is_high:
            if (p.is_high and p.price > filtered[-1].price) or \
               (not p.is_high and p.price < filtered[-1].price):
                filtered[-1] = p
        else:
            filtered.append(p)
    return filtered


# ══════════════════════════════════════════════════════════════
# 엔진 2: 패턴 식별 + 절대법칙 필터
# ══════════════════════════════════════════════════════════════

def _fib_match(ratio: float, targets: list[float], tol: float = None) -> bool:
    if tol is None: tol = R.fib_tolerance
    return any(abs(ratio - t) / max(t, 0.001) <= tol for t in targets)


def _wave_size(start: Pivot, end: Pivot) -> float:
    return abs(end.price - start.price)


def check_impulse_absolute_laws(p0, p1, p2, p3, p4, p5, direction: str) -> tuple[bool, float, str]:
    """
    임펄스 절대법칙 검증 (지침서 엔진2)

    절대 법칙:
    1. 3파가 가장 짧으면 즉시 기각
    2. 2파 되돌림 ≤ 1파의 61.8% (절대 초과 금지)
    3. 연장의 법칙: 가장 긴 파동 ≥ 두 번째로 긴 파동 × 1.618
    4. 4파는 1파 영역과 겹치지 않음

    반환: (통과여부, 신뢰도, 기각사유)
    """
    if direction == "up":
        w1 = p1.price - p0.price
        w2 = p1.price - p2.price   # 조정 크기
        w3 = p3.price - p2.price
        w4 = p3.price - p4.price
        w5 = p5.price - p4.price if p5 else 0
        wave4_end = p4.price
        wave1_end = p1.price
        # 4파 비겹침
        no_overlap = wave4_end > p0.price  # 1파 시작점 위
    else:
        w1 = p0.price - p1.price
        w2 = p2.price - p1.price
        w3 = p2.price - p3.price
        w4 = p4.price - p3.price
        w5 = p4.price - p5.price if p5 else 0
        wave4_end = p4.price
        wave1_end = p1.price
        no_overlap = wave4_end < p0.price

    score = 0
    reasons = []

    # 절대법칙 1: 3파가 가장 짧으면 즉시 기각
    if w3 < min(w1, w5) if w5 > 0 else w3 < w1:
        return False, 0.0, "3파가 가장 짧음 (절대 기각)"

    # 절대법칙 2: 2파 되돌림 ≤ 61.8%
    if w1 > 0 and (w2 / w1) > R.w2_max_retracement + R.fib_tolerance:
        return False, 0.0, f"2파 되돌림 과다: {w2/w1:.1%} > {R.w2_max_retracement:.1%} (절대 기각)"

    score += 1  # 절대법칙 2 통과

    # 절대법칙 3: 연장의 법칙
    waves = sorted([w1, w3, w5], reverse=True) if w5 > 0 else sorted([w1, w3], reverse=True)
    if len(waves) >= 2 and waves[1] > 0:
        ext_ratio = waves[0] / waves[1]
        if ext_ratio >= R.extension_law_ratio * (1 - R.fib_tolerance):
            score += 1
        else:
            reasons.append(f"연장비율 부족: {ext_ratio:.2f}")

    # 절대법칙 4: 4파 비겹침
    if no_overlap:
        score += 1
    else:
        return False, 0.0, "4파가 1파 영역과 겹침 (절대 기각)"

    # 피보나치 비율 검증
    if w1 > 0:
        r2 = w2 / w1
        if _fib_match(r2, [FIB_382, FIB_500, FIB_618]):
            score += 1
        if w3 > 0 and _fib_match(w3 / w1, [FIB_1272, FIB_1618, FIB_2618]):
            score += 1
        if w5 > 0 and _fib_match(w5 / w1, [FIB_618, FIB_1000, FIB_1272]):
            score += 1

    confidence = score / 6.0
    return True, confidence, ""


def check_zigzag_laws(p0, pA, pB, pC, direction: str) -> tuple[bool, float, str]:
    """
    지그재그 절대법칙 (5-3-5 구조)
    B파 ≤ A파의 61.8% (지침서: B파가 A파의 0.618 이하)
    """
    if direction == "down":
        wA = p0.price - pA.price
        wB = pB.price - pA.price
        wC = pB.price - pC.price
    else:
        wA = pA.price - p0.price
        wB = pA.price - pB.price
        wC = pC.price - pB.price

    if wA <= 0:
        return False, 0.0, "A파 크기 오류"

    # 절대법칙: B파 ≤ A파의 61.8%
    b_ratio = wB / wA
    if b_ratio > R.fib_tolerance + R.get("피보나치_비율.zigzag_b_max", FIB_618):
        return False, 0.0, f"지그재그: B파 되돌림 과다 {b_ratio:.1%}"

    score = 0
    if _fib_match(b_ratio, [FIB_382, FIB_500, FIB_618]):
        score += 1
    if _fib_match(wC / wA, [FIB_618, FIB_1000, FIB_1272, FIB_1618]):
        score += 1
    if direction == "down" and pC.price < pA.price:
        score += 1
    elif direction == "up" and pC.price > pA.price:
        score += 1

    return True, score / 3.0, ""


def check_flat_laws(p0, pA, pB, pC, direction: str) -> tuple[bool, float, str]:
    """
    플랫 절대법칙 (3-3-5 구조)
    B파 ≥ A파의 61.8%
    """
    if direction == "down":
        wA = p0.price - pA.price
        wB = pB.price - pA.price
        wC = pB.price - pC.price
    else:
        wA = pA.price - p0.price
        wB = pA.price - pB.price
        wC = pC.price - pB.price

    if wA <= 0:
        return False, 0.0, "A파 오류"

    b_ratio = wB / wA
    # B파 ≥ 61.8% (플랫 조건)
    flat_b_min = R.get("피보나치_비율.flat_b_min", FIB_618)
    if b_ratio < flat_b_min - R.fib_tolerance:
        return False, 0.0, f"플랫: B파 되돌림 부족 {b_ratio:.1%} < {flat_b_min:.1%}"

    score = 0
    # 일반형: 0.81~1.0
    if _fib_match(b_ratio, [FIB_786, FIB_886, FIB_1000]):
        score += 1
    # 불규칙형: 1.0~1.382
    elif _fib_match(b_ratio, [FIB_1000, FIB_1272, FIB_1382]):
        score += 1

    if _fib_match(wC / wA, [FIB_1000, FIB_1272, FIB_1618]):
        score += 1

    return True, score / 2.0, ""


def check_terminal_laws(p0, p1, p2, p3, p4, direction: str) -> tuple[bool, float, str]:
    """
    터미널 충격파(다이아고날/엔딩 웨지) 절대법칙 (지침서 모듈2)

    - 모든 내부 파동이 3구조 (여기선 진폭 비율로 근사)
    - 2파와 4파 가격대가 반드시 겹쳐야 함 (임펄스와 반대)
    - 각 파동이 이전보다 작아지는 수렴 구조
    - 1파 연장 터미널: 5번 파동이 1-3 추세선을 이탈(fakeout)해야 완성
    """
    if direction == "up":
        w1 = p1.price - p0.price
        w2 = p1.price - p2.price   # 2파 조정 크기
        w3 = p3.price - p2.price
        w4 = p3.price - p4.price   # 4파 조정 크기
        # 2파와 4파 겹침 필수: 4파 저점이 1파 고점보다 낮아야 함
        overlap = p4.price < p1.price
    else:
        w1 = p0.price - p1.price
        w2 = p2.price - p1.price
        w3 = p2.price - p3.price
        w4 = p4.price - p3.price
        # 2파와 4파 겹침: 4파 고점이 1파 저점보다 높아야 함
        overlap = p4.price > p1.price

    # 절대법칙: 2파와 4파 겹침 필수
    if not overlap:
        return False, 0.0, "터미널: 2파-4파 겹침 없음 (기각)"

    if w1 <= 0 or w2 <= 0 or w3 <= 0 or w4 <= 0:
        return False, 0.0, "터미널: 파동 크기 오류"

    score = 0

    # 수렴 검증: 각 충격파가 이전보다 작아야 함 (수렴형)
    if w3 < w1:
        score += 2
    # 조정파도 수렴 경향
    if w4 < w2:
        score += 1

    # 피보나치 비율: 2파는 1파의 61.8~88.6% 되돌림
    r2 = w2 / w1
    if _fib_match(r2, [FIB_618, FIB_786, FIB_886]):
        score += 2

    # 3파는 1파의 61.8~100% (수렴이므로 1파보다 작거나 비슷)
    r3 = w3 / w1
    if _fib_match(r3, [FIB_618, FIB_786, FIB_1000]):
        score += 1

    confidence = min(0.50 + score * 0.07, 0.85)
    return True, confidence, ""


def check_triangle_laws(pivots: list) -> tuple[bool, float, str]:
    """
    삼각형(3-3-3-3-3) 절대법칙 (지침서 모듈2)

    - 최소 3개의 내부 파동이 직전 파동의 50% 이상을 되돌려야 함
    - E파(마지막 파동)가 가격상 가장 짧아야 함
    - B-D 추세선 사용 (0-B 추세선 절대 금지)
    - 수렴형: 각 파동이 점점 작아지는 경향
    """
    if len(pivots) < 5:
        return False, 0.0, "삼각형: 피벗 부족 (최소 5개 필요)"

    p = pivots[-5:]
    wA = abs(p[1].price - p[0].price)
    wB = abs(p[2].price - p[1].price)
    wC = abs(p[3].price - p[2].price)
    wD = abs(p[4].price - p[3].price)

    waves = [wA, wB, wC, wD]
    if any(w <= 0 for w in waves):
        return False, 0.0, "삼각형: 파동 크기 오류"

    # 최소 3개 내부 파동이 직전 파동의 50% 이상 되돌림
    retrace_count = sum(
        1 for i in range(1, 4) if waves[i] >= waves[i - 1] * 0.50
    )
    if retrace_count < 3:
        return False, 0.0, f"삼각형: 되돌림 조건 미달 ({retrace_count}/3)"

    score = retrace_count  # 3~4점

    # E파(wD)가 가격상 가장 짧아야 함
    if wD <= min(wA, wB, wC):
        score += 2

    # 수렴형 확인: A > B > C or B > C > D
    if wA > wB and wC > wD:
        score += 1
    if wB > wC and wC > wD:
        score += 1

    confidence = min(0.50 + score * 0.06, 0.88)
    return True, confidence, ""


# ══════════════════════════════════════════════════════════════
# 엔진 3: 추세선 트리거
# ══════════════════════════════════════════════════════════════

def calculate_trendline_value(x1: int, y1: float, x2: int, y2: float, x: int) -> float:
    """두 점을 잇는 추세선의 x 위치에서의 y값"""
    if x2 == x1:
        return y1
    slope = (y2 - y1) / (x2 - x1)
    return y1 + slope * (x - x1)


def check_trendlines(pivots: list[Pivot], current_price: float, current_idx: int) -> TrendlineResult:
    """
    2-4 추세선 및 B-D 추세선 분석
    """
    result = TrendlineResult()
    if len(pivots) < 4:
        return result

    # 상승 임펄스 2-4 추세선: 저점들(2파, 4파) 연결
    lows  = [p for p in pivots if not p.is_high]
    highs = [p for p in pivots if p.is_high]

    if len(lows) >= 2:
        p2, p4 = lows[-2], lows[-1]
        line_val = calculate_trendline_value(p2.index, p2.price, p4.index, p4.price, current_idx)
        result.has_24_line = True
        result.line_24_price = line_val
        # 현재가가 추세선을 하향 돌파 → 임펄스 마감
        result.broken_24 = current_price < line_val * (1 - 0.001)

    if len(highs) >= 2:
        pB, pD = highs[-2], highs[-1]
        line_val = calculate_trendline_value(pB.index, pB.price, pD.index, pD.price, current_idx)
        result.has_bd_line = True
        result.line_bd_price = line_val
        # B-D 추세선 상향 돌파 → 삼각형 분출
        result.broken_bd = current_price > line_val * (1 + 0.001)

    return result


# ══════════════════════════════════════════════════════════════
# 엔진 4: PRG (Potential Reversal Zone) 피보나치 중첩
# ══════════════════════════════════════════════════════════════

def calculate_prg(pivots: list[Pivot], direction: str) -> PRGZone:
    """
    여러 피보나치 비율을 동시에 계산하여
    가장 밀집된 가격대(PRG)를 반환

    direction: "up" (반등 예상) or "down" (하락 예상)
    """
    prg = PRGZone(0, 0, 0)
    if len(pivots) < 3:
        return prg

    fib_levels = []

    # 최근 스윙에 대한 피보나치 되돌림/연장 계산
    recent = pivots[-6:]
    swings = []
    for i in range(len(recent) - 1):
        swings.append((recent[i], recent[i + 1]))

    for start, end in swings:
        swing_size = abs(end.price - start.price)
        if swing_size < 0.0001:
            continue

        base = end.price  # 되돌림 기준점

        if direction == "up":
            # 하락 스윙의 되돌림 레벨 (지지 예상)
            if start.is_high and not end.is_high:
                for ratio in [FIB_236, FIB_382, FIB_500, FIB_618, FIB_786]:
                    fib_levels.append(end.price + swing_size * ratio)
        else:
            # 상승 스윙의 되돌림 레벨 (저항 예상)
            if not start.is_high and end.is_high:
                for ratio in [FIB_236, FIB_382, FIB_500, FIB_618, FIB_786]:
                    fib_levels.append(end.price - swing_size * ratio)

    _prg_min = R.min_prg_confluence
    if len(fib_levels) < _prg_min:
        return prg

    # 클러스터링: 서로 1% 이내 레벨들을 그룹화
    fib_levels.sort()
    best_cluster = []
    best_count = 0

    for i, level in enumerate(fib_levels):
        cluster = [l for l in fib_levels if abs(l - level) / level <= 0.01]
        if len(cluster) > best_count:
            best_count = len(cluster)
            best_cluster = cluster

    if best_count >= _prg_min:
        prg.price_low  = min(best_cluster)
        prg.price_high = max(best_cluster)
        prg.confluence_count = best_count
        prg.fib_levels = best_cluster
        prg.valid = True

    return prg


def is_price_in_prg(current_price: float, prg: PRGZone, buffer_pct: float = 0.005) -> bool:
    """현재가가 PRG 구간 안에 있는지 확인"""
    if not prg.valid:
        return False
    low  = prg.price_low  * (1 - buffer_pct)
    high = prg.price_high * (1 + buffer_pct)
    return low <= current_price <= high


# ══════════════════════════════════════════════════════════════
# 메인 파동 분석 함수
# ══════════════════════════════════════════════════════════════

def analyze_wave(df: pd.DataFrame, pivot_left: int = None, pivot_right: int = None) -> WaveCount:
    if pivot_left is None:  pivot_left  = R.pivot_left
    if pivot_right is None: pivot_right = R.pivot_right
    """
    지침서 기반 종합 파동 분석

    우선순위:
    1. Wave 4 완성 → Wave 5 진입 (임펄스, 가장 강력)
    2. Wave 2 완성 → Wave 3 진입 (임펄스, 가장 수익)
    3. Wave B 완성 → Wave C 진입 (조정)
    """
    result = WaveCount()

    pivots = detect_pivots(df, pivot_left, pivot_right)
    if len(pivots) < 4:
        return result

    result.pivots = pivots
    current_price = float(df["close"].iloc[-1])
    current_idx   = len(df) - 1

    # 추세선 분석
    result.trendline = check_trendlines(pivots, current_price, current_idx)

    recent = pivots[-10:]

    # ──────────────────────────────────────────────────────────
    # 패턴 1: 상승 임펄스 Wave 5 진입 (4파 완성 확인)
    # p0(저) → p1(고) → p2(저) → p3(고) → p4(저) → Wave5 진입
    # ──────────────────────────────────────────────────────────
    for i in range(len(recent) - 4):
        p = recent[i:i+5]
        if (not p[0].is_high and p[1].is_high and
            not p[2].is_high and p[3].is_high and not p[4].is_high):

            mock_p5 = Pivot(current_idx, current_price, True)
            valid, conf, reason = check_impulse_absolute_laws(
                p[0], p[1], p[2], p[3], p[4], mock_p5, "up"
            )
            if valid and conf >= R.min_confidence:
                prg = calculate_prg(list(p), "up")
                result.wave_type          = WaveType.IMPULSE
                result.current_position   = WavePosition.WAVE5
                result.direction          = "up"
                result.fib_valid          = True
                result.entry_zone         = True
                result.target_price       = _wave5_target(p[0], p[1], p[2], p[3], p[4], "up")
                result.invalidation_price = p[4].price
                result.confidence         = conf
                result.prg                = prg
                result.abs_law_passed     = True
                return result

    # ──────────────────────────────────────────────────────────
    # 패턴 2: 하락 임펄스 Wave 5 진입
    # ──────────────────────────────────────────────────────────
    for i in range(len(recent) - 4):
        p = recent[i:i+5]
        if (p[0].is_high and not p[1].is_high and
            p[2].is_high and not p[3].is_high and p[4].is_high):

            mock_p5 = Pivot(current_idx, current_price, False)
            valid, conf, reason = check_impulse_absolute_laws(
                p[0], p[1], p[2], p[3], p[4], mock_p5, "down"
            )
            if valid and conf >= R.min_confidence:
                prg = calculate_prg(list(p), "down")
                result.wave_type          = WaveType.IMPULSE
                result.current_position   = WavePosition.WAVE5
                result.direction          = "down"
                result.fib_valid          = True
                result.entry_zone         = True
                result.target_price       = _wave5_target(p[0], p[1], p[2], p[3], p[4], "down")
                result.invalidation_price = p[4].price
                result.confidence         = conf
                result.prg                = prg
                result.abs_law_passed     = True
                return result

    # ──────────────────────────────────────────────────────────
    # 패턴 3: 상승 Wave 3 진입 (Wave 2 완성)
    # p0(저) → p1(고) → p2(저) → Wave3 진입
    # ──────────────────────────────────────────────────────────
    for i in range(len(recent) - 2):
        p = recent[i:i+3]
        if not p[0].is_high and p[1].is_high and not p[2].is_high:
            w1 = p[1].price - p[0].price
            w2 = p[1].price - p[2].price
            if w1 <= 0:
                continue
            r2 = w2 / w1
            # 2파 절대법칙: ≤ 61.8%
            if r2 > R.w2_max_retracement + R.fib_tolerance:
                continue
            if not _fib_match(r2, [FIB_382, FIB_500, FIB_618]):
                continue
            if p[2].price <= p[0].price:  # 2파가 1파 시작점 이하
                continue

            prg = calculate_prg(list(p), "up")
            conf = 0.6 + (0.1 if prg.valid else 0)
            result.wave_type          = WaveType.IMPULSE
            result.current_position   = WavePosition.WAVE3
            result.direction          = "up"
            result.fib_valid          = True
            result.entry_zone         = True
            result.target_price       = p[2].price + w1 * FIB_1618
            result.invalidation_price = p[0].price
            result.confidence         = conf
            result.prg                = prg
            result.abs_law_passed     = True
            return result

    # ──────────────────────────────────────────────────────────
    # 패턴 4: 하락 Wave 3 진입
    # ──────────────────────────────────────────────────────────
    for i in range(len(recent) - 2):
        p = recent[i:i+3]
        if p[0].is_high and not p[1].is_high and p[2].is_high:
            w1 = p[0].price - p[1].price
            w2 = p[2].price - p[1].price
            if w1 <= 0:
                continue
            r2 = w2 / w1
            if r2 > R.w2_max_retracement + R.fib_tolerance:
                continue
            if not _fib_match(r2, [FIB_382, FIB_500, FIB_618]):
                continue
            if p[2].price >= p[0].price:
                continue

            prg = calculate_prg(list(p), "down")
            conf = 0.6 + (0.1 if prg.valid else 0)
            result.wave_type          = WaveType.IMPULSE
            result.current_position   = WavePosition.WAVE3
            result.direction          = "down"
            result.fib_valid          = True
            result.entry_zone         = True
            result.target_price       = p[2].price - w1 * FIB_1618
            result.invalidation_price = p[0].price
            result.confidence         = conf
            result.prg                = prg
            result.abs_law_passed     = True
            return result

    # ──────────────────────────────────────────────────────────
    # 패턴 5: 지그재그 Wave C 진입 (하락 조정)
    # ──────────────────────────────────────────────────────────
    for i in range(len(recent) - 3):
        p = recent[i:i+4]
        if (p[0].is_high and not p[1].is_high and p[2].is_high and not p[3].is_high):
            valid, conf, reason = check_zigzag_laws(p[0], p[1], p[2], p[3], "down")
            if valid and conf >= R.min_confidence:
                wA = p[0].price - p[1].price
                prg = calculate_prg(list(p), "down")
                result.wave_type          = WaveType.ZIGZAG
                result.current_position   = WavePosition.WAVE_C
                result.direction          = "down"
                result.fib_valid          = True
                result.entry_zone         = True
                result.target_price       = p[2].price - wA
                result.invalidation_price = p[2].price
                result.confidence         = conf
                result.prg                = prg
                result.abs_law_passed     = True
                return result

    # ──────────────────────────────────────────────────────────
    # 패턴 6: 지그재그 Wave C 진입 (상승 조정)
    # ──────────────────────────────────────────────────────────
    for i in range(len(recent) - 3):
        p = recent[i:i+4]
        if (not p[0].is_high and p[1].is_high and not p[2].is_high and p[3].is_high):
            valid, conf, reason = check_zigzag_laws(p[0], p[1], p[2], p[3], "up")
            if valid and conf >= R.min_confidence:
                wA = p[1].price - p[0].price
                prg = calculate_prg(list(p), "up")
                result.wave_type          = WaveType.ZIGZAG
                result.current_position   = WavePosition.WAVE_C
                result.direction          = "up"
                result.fib_valid          = True
                result.entry_zone         = True
                result.target_price       = p[2].price + wA
                result.invalidation_price = p[2].price
                result.confidence         = conf
                result.prg                = prg
                result.abs_law_passed     = True
                return result

    # ──────────────────────────────────────────────────────────
    # 패턴 7: 플랫 Wave C 진입 (하락 조정)
    # ──────────────────────────────────────────────────────────
    for i in range(len(recent) - 3):
        p = recent[i:i+4]
        if (p[0].is_high and not p[1].is_high and p[2].is_high and not p[3].is_high):
            valid, conf, reason = check_flat_laws(p[0], p[1], p[2], p[3], "down")
            if valid and conf >= R.min_confidence:
                wA = p[0].price - p[1].price
                prg = calculate_prg(list(p), "down")
                result.wave_type          = WaveType.FLAT
                result.current_position   = WavePosition.WAVE_C
                result.direction          = "down"
                result.fib_valid          = True
                result.entry_zone         = True
                result.target_price       = p[2].price - wA * FIB_1000
                result.invalidation_price = p[2].price
                result.confidence         = conf
                result.prg                = prg
                result.abs_law_passed     = True
                return result

    # ──────────────────────────────────────────────────────────
    # 패턴 8: 플랫 Wave C 진입 (상승 조정 — 하락 플랫의 반대)
    # p0(저) → pA(고) → pB(저) → Wave C 상승 진입
    # ──────────────────────────────────────────────────────────
    for i in range(len(recent) - 3):
        p = recent[i:i+4]
        if (not p[0].is_high and p[1].is_high and not p[2].is_high and p[3].is_high):
            valid, conf, reason = check_flat_laws(p[0], p[1], p[2], p[3], "up")
            if valid and conf >= R.min_confidence:
                wA = p[1].price - p[0].price
                prg = calculate_prg(list(p), "up")
                result.wave_type          = WaveType.FLAT
                result.current_position   = WavePosition.WAVE_C
                result.direction          = "up"
                result.fib_valid          = True
                result.entry_zone         = True
                result.target_price       = p[2].price + wA * FIB_1000
                result.invalidation_price = p[2].price
                result.confidence         = conf
                result.prg                = prg
                result.abs_law_passed     = True
                return result

    # ──────────────────────────────────────────────────────────
    # 패턴 9: 상승 터미널 충격파 완성 → 숏 반전 진입
    # p0(L)→p1(H)→p2(L)→p3(H)→p4(L): 4파가 1파 고점 하향 겹침
    # 터미널 완성 = 큰 반전 신호 → 공매도 진입
    # ──────────────────────────────────────────────────────────
    for i in range(len(recent) - 4):
        p = recent[i:i+5]
        if (not p[0].is_high and p[1].is_high and
                not p[2].is_high and p[3].is_high and not p[4].is_high):
            valid, conf, reason = check_terminal_laws(
                p[0], p[1], p[2], p[3], p[4], "up"
            )
            if valid and conf >= R.min_confidence:
                # 2-4 추세선 돌파 여부 (터미널 마감 확인)
                tl = check_trendlines(list(p), current_price, current_idx)
                if not tl.broken_24:
                    continue  # 아직 2-4 추세선 미돌파 → 터미널 미완성
                prg = calculate_prg(list(p), "down")
                result.wave_type          = WaveType.TERMINAL
                result.current_position   = WavePosition.WAVE5
                result.direction          = "down"      # 터미널 완성 → 강한 반전 하락
                result.fib_valid          = True
                result.entry_zone         = True
                result.target_price       = _terminal_reversal_tp(p[0])
                result.invalidation_price = p[3].price  # 3파 고점 위에서 SL
                result.confidence         = conf
                result.prg                = prg
                result.trendline          = tl
                result.abs_law_passed     = True
                return result

    # ──────────────────────────────────────────────────────────
    # 패턴 10: 하락 터미널 충격파 완성 → 롱 반전 진입
    # p0(H)→p1(L)→p2(H)→p3(L)→p4(H): 4파가 1파 저점 상향 겹침
    # ──────────────────────────────────────────────────────────
    for i in range(len(recent) - 4):
        p = recent[i:i+5]
        if (p[0].is_high and not p[1].is_high and
                p[2].is_high and not p[3].is_high and p[4].is_high):
            valid, conf, reason = check_terminal_laws(
                p[0], p[1], p[2], p[3], p[4], "down"
            )
            if valid and conf >= R.min_confidence:
                tl = check_trendlines(list(p), current_price, current_idx)
                if not tl.broken_24:
                    continue
                prg = calculate_prg(list(p), "up")
                result.wave_type          = WaveType.TERMINAL
                result.current_position   = WavePosition.WAVE5
                result.direction          = "up"        # 하락 터미널 완성 → 강한 반전 상승
                result.fib_valid          = True
                result.entry_zone         = True
                result.target_price       = _terminal_reversal_tp(p[0])
                result.invalidation_price = p[3].price  # 3파 저점 아래에서 SL
                result.confidence         = conf
                result.prg                = prg
                result.trendline          = tl
                result.abs_law_passed     = True
                return result

    # ──────────────────────────────────────────────────────────
    # 패턴 11: 삼각형 수렴 + B-D 추세선 상향 돌파 → 롱
    # 5개 교대 피벗 (ABCDE), B-D 추세선 상향 이탈 = 상방 분출
    # ──────────────────────────────────────────────────────────
    if len(recent) >= 5:
        tri_pivots = recent[-5:]
        # 수렴형 하락 삼각형: H-L-H-L-H (마지막 H 이후 상방 분출)
        if (tri_pivots[0].is_high and not tri_pivots[1].is_high and
                tri_pivots[2].is_high and not tri_pivots[3].is_high and
                tri_pivots[4].is_high):
            valid, conf, reason = check_triangle_laws(list(tri_pivots))
            if valid and conf >= R.min_confidence:
                tl = check_trendlines(list(tri_pivots), current_price, current_idx)
                if tl.broken_bd:  # B-D 추세선 상향 돌파
                    prg = calculate_prg(list(tri_pivots), "up")
                    tp  = _triangle_breakout_tp(list(tri_pivots), current_price, "up")
                    result.wave_type          = WaveType.TRIANGLE
                    result.current_position   = WavePosition.COMPLETE
                    result.direction          = "up"
                    result.fib_valid          = True
                    result.entry_zone         = True
                    result.target_price       = tp
                    result.invalidation_price = tri_pivots[-1].price * 0.998
                    result.confidence         = conf
                    result.prg                = prg
                    result.trendline          = tl
                    result.abs_law_passed     = True
                    return result

    # ──────────────────────────────────────────────────────────
    # 패턴 12: 삼각형 수렴 + B-D 추세선 하향 돌파 → 숏
    # 5개 교대 피벗 (ABCDE), B-D 추세선 하향 이탈 = 하방 분출
    # ──────────────────────────────────────────────────────────
    if len(recent) >= 5:
        tri_pivots = recent[-5:]
        # 수렴형 상승 삼각형: L-H-L-H-L (마지막 L 이후 하방 분출)
        if (not tri_pivots[0].is_high and tri_pivots[1].is_high and
                not tri_pivots[2].is_high and tri_pivots[3].is_high and
                not tri_pivots[4].is_high):
            valid, conf, reason = check_triangle_laws(list(tri_pivots))
            if valid and conf >= R.min_confidence:
                tl = check_trendlines(list(tri_pivots), current_price, current_idx)
                if tl.broken_24:  # 저점 추세선 하향 돌파
                    prg = calculate_prg(list(tri_pivots), "down")
                    tp  = _triangle_breakout_tp(list(tri_pivots), current_price, "down")
                    result.wave_type          = WaveType.TRIANGLE
                    result.current_position   = WavePosition.COMPLETE
                    result.direction          = "down"
                    result.fib_valid          = True
                    result.entry_zone         = True
                    result.target_price       = tp
                    result.invalidation_price = tri_pivots[-1].price * 1.002
                    result.confidence         = conf
                    result.prg                = prg
                    result.trendline          = tl
                    result.abs_law_passed     = True
                    return result

    return result


def _wave5_target(p0, p1, p2, p3, p4, direction: str) -> float:
    """Wave 5 목표가: Wave 1 크기의 1.0배"""
    w1 = abs(p1.price - p0.price)
    if direction == "up":
        return p4.price + w1
    return p4.price - w1


def _determine_extension_type(w1: float, w3: float, w5: float) -> str:
    """
    임펄스 파동에서 연장된 파동 식별 (지침서 모듈2)
    가장 긴 파동이 두 번째로 긴 파동의 1.618배 이상 → 그 파동이 연장됨
    반환: "1" | "3" | "5" | "none"
    """
    candidates = [("1", w1), ("3", w3)]
    if w5 > 0:
        candidates.append(("5", w5))
    candidates.sort(key=lambda x: x[1], reverse=True)

    if len(candidates) >= 2 and candidates[1][1] > 0:
        if candidates[0][1] / candidates[1][1] >= FIB_1618 * (1 - 0.08):
            return candidates[0][0]
    return "none"


def _impulse_reversal_tp(p0, p1, p2, p3, p4, direction: str) -> float:
    """
    임펄스 완성 후 반전 목표가 (지침서 모듈5)
    - 1파 연장: 직전 2파 영역 (p2) 되돌림
    - 3파 연장: 직전 4파 영역 (p4) 되돌림
    - 5파 연장: 전체 임펄스의 0.618 되돌림
    - 기타: 전체의 0.618 되돌림
    """
    if direction == "up":
        w1 = p1.price - p0.price
        w3 = p3.price - p2.price
        w5 = 0.0
        ext = _determine_extension_type(w1, w3, w5)
        if ext == "1":
            return p2.price            # 2파 영역
        elif ext == "3":
            return p4.price            # 4파 영역
        else:
            total = p3.price - p0.price  # 전체 임펄스 추정
            return p3.price - total * FIB_618
    else:
        w1 = p0.price - p1.price
        w3 = p2.price - p3.price
        w5 = 0.0
        ext = _determine_extension_type(w1, w3, w5)
        if ext == "1":
            return p2.price
        elif ext == "3":
            return p4.price
        else:
            total = p0.price - p3.price
            return p3.price + total * FIB_618


def _terminal_reversal_tp(p0_terminal: "Pivot") -> float:
    """
    터미널 충격파 완성 후 목표가 (지침서 모듈5)
    터미널 형성 시간의 50% 이내에 시작점 100% 되돌려야 함
    → 시작점(p0)이 목표가
    """
    return p0_terminal.price


def _triangle_breakout_tp(pivots: list, breakout_price: float, direction: str) -> float:
    """
    삼각형 돌파 후 목표가 (지침서 모듈5)
    삼각형 내 가장 큰 파동의 100% (범위: 75%~125%)
    → 보통 100% 도달 시 주력 청산
    """
    if len(pivots) < 5:
        return breakout_price
    p = pivots[-5:]
    waves = [abs(p[i + 1].price - p[i].price) for i in range(4)]
    max_wave = max(waves) if waves else 0.0
    if direction == "up":
        return breakout_price + max_wave
    return breakout_price - max_wave


def get_trend_direction(df: pd.DataFrame) -> str:
    """추세 방향 판단 (상위 TF용)"""
    pivots = detect_pivots(df, R.pivot_left + 2, R.pivot_right + 2)
    if len(pivots) < 4:
        return "sideways"

    recent = pivots[-4:]
    highs = [p.price for p in recent if p.is_high]
    lows  = [p.price for p in recent if not p.is_high]

    if len(highs) < 2 or len(lows) < 2:
        return "sideways"

    if highs[-1] > highs[-2] and lows[-1] > lows[-2]:
        return "up"
    if highs[-1] < highs[-2] and lows[-1] < lows[-2]:
        return "down"
    return "sideways"
