"""
5개 타임프레임 계층 구조 매매 신호 생성

계층 구조:
  4h  → 최상위 추세 방향 (Big Degree)
  1h  → 중기 파동 방향 확인
  15m → 세부 파동 필터
  5m  → 메인 매매 로직 (진입 구간 판단)
  1m  → 정밀 진입 타점

레버리지 결정:
  기본  4x  : 파동 단독 신호
  중간  7x  : PRG 또는 RSI 다이버전스 단독 확인
  최대 10x  : PRG + RSI 다이버전스 동시 확인
"""

from dataclasses import dataclass, field
from typing import Optional
import logging
from wave_analyzer import (
    analyze_wave, get_trend_direction,
    WavePosition, WaveType, is_price_in_prg
)
from indicators import (
    detect_rsi_divergence, get_rsi_zone,
    divergence_aligns_with_signal, DivergenceResult
)
from config import (
    TF_4H, TF_1H, TF_15M, TF_5M, TF_1M,
    SLIPPAGE_BUFFERS, SLIPPAGE_DEFAULT_PCT,
)
from rule_loader import R

logger = logging.getLogger(__name__)


@dataclass
class TradeSignal:
    symbol: str
    side: str                   # "long" | "short"
    entry_price: float          # 호가 양보 적용 지정가
    stop_loss: float
    take_profit: float
    confidence: float
    leverage: int               # 동적 레버리지 (4/7/10)
    wave_position: str
    wave_type: str
    trend_4h: str
    trend_1h: str
    trend_15m: str
    wave_5m_dir: str
    reason: str
    entry_reason: str = ""
    rsi_confirmed: bool = False
    prg_confirmed: bool = False


# ══════════════════════════════════════════════════════════════
# 호가 양보 시스템 (지침서 엔진5)
# ══════════════════════════════════════════════════════════════

def apply_slippage_buffer(price: float, side: str) -> float:
    """
    가격대별 호가 양보 (지정가 주문 앞당기기)
    롱: 진입가를 buffer만큼 올려서 체결 확률 높임
    숏: 진입가를 buffer만큼 내려서 체결 확률 높임
    """
    buffer = SLIPPAGE_DEFAULT_PCT * price

    for low, high, buf_usd in SLIPPAGE_BUFFERS:
        if low <= price < high:
            buffer = buf_usd
            break

    if side == "long":
        return price + buffer
    return price - buffer


# ══════════════════════════════════════════════════════════════
# TP 계산 (소모닉 0.618 되돌림 고정, 지침서 엔진5)
# ══════════════════════════════════════════════════════════════

def calculate_wave_tp(entry_price: float, target_price: float, side: str) -> float:
    """
    파동 목표가 기반 익절
    - 파동 목표가(target_price)를 그대로 사용 (0.618 축소 없음)
    - 하한: SL × min_rr (손익비 보장)
    - 상한: max_tp_pct (무제한 방지)
    """
    # 최소 TP = SL% × min_rr (예: 2% × 1.5 = 3%)
    min_tp = max(R.min_tp_pct, R.stop_loss_pct * R.min_rr_ratio)
    max_tp = R.max_tp_pct

    if entry_price <= 0:
        return entry_price

    if target_price <= 0:
        # 파동 목표가 없으면 최소 TP 적용
        return round(entry_price * (1 + min_tp), 6) if side == "long" \
               else round(entry_price * (1 - min_tp), 6)

    if side == "long":
        raw_pct = (target_price - entry_price) / entry_price
        if raw_pct <= min_tp:
            # 목표가가 너무 가까우면 최소 TP 보장
            return round(entry_price * (1 + min_tp), 6)
        return round(entry_price * (1 + min(max_tp, raw_pct)), 6)
    else:
        raw_pct = (entry_price - target_price) / entry_price
        if raw_pct <= min_tp:
            return round(entry_price * (1 - min_tp), 6)
        return round(entry_price * (1 - min(max_tp, raw_pct)), 6)


def calculate_somonics_tp(entry_price: float, target_price: float, side: str) -> float:
    """calculate_wave_tp 호환 래퍼"""
    return calculate_wave_tp(entry_price, target_price, side)


# ══════════════════════════════════════════════════════════════
# 동적 레버리지 결정
# ══════════════════════════════════════════════════════════════

def decide_leverage(prg_confirmed: bool, rsi_confirmed: bool, confidence: float) -> int:
    """
    PRG + RSI 다이버전스 동시 확인 → 10x
    둘 중 하나만 → 7x
    둘 다 없음   → 4x
    """
    if prg_confirmed and rsi_confirmed:
        return R.leverage_max
    elif prg_confirmed or rsi_confirmed:
        return R.leverage_medium
    return R.leverage_default


# ══════════════════════════════════════════════════════════════
# 메인 신호 생성
# ══════════════════════════════════════════════════════════════

def generate_signal(symbol: str, all_tf: dict) -> Optional[TradeSignal]:
    """
    5개 TF 계층 분석으로 매매 신호 생성

    Args:
        symbol : 코인 심볼
        all_tf : {"4h": df, "1h": df, "15m": df, "5m": df, "1m": df}
    """
    required = [TF_4H, TF_1H, TF_15M, TF_5M, TF_1M]
    if not all_tf or any(tf not in all_tf for tf in required):
        return None

    # ─── 1. 상위 추세 분석 (4h → 1h → 15m) ─────────────────────
    trend_4h  = get_trend_direction(all_tf[TF_4H])
    trend_1h  = get_trend_direction(all_tf[TF_1H])
    trend_15m = get_trend_direction(all_tf[TF_15M])

    logger.info(f"[{symbol}] 추세: 4h={trend_4h} | 1h={trend_1h} | 15m={trend_15m}")

    # 4h + 1h 모두 횡보면 스킵 (4h 횡보 단독은 허용 — 1h가 명확하면 진입 가능)
    if trend_4h == "sideways" and trend_1h == "sideways":
        logger.info(f"[{symbol}] 4h+1h 모두 횡보 → 스킵")
        return None

    # ─── 2. 5m 메인 파동 분석 ───────────────────────────────────
    wave_5m = analyze_wave(all_tf[TF_5M])

    if not wave_5m.entry_zone:
        logger.info(f"[{symbol}] 5m 진입 구간 아님 (위치:{wave_5m.current_position})")
        return None

    if not wave_5m.abs_law_passed:
        logger.info(f"[{symbol}] 5m 절대법칙 미통과: {wave_5m.rejection_reason}")
        return None

    if wave_5m.confidence < R.min_confidence:
        logger.info(f"[{symbol}] 5m 신뢰도 부족: {wave_5m.confidence:.0%} < {R.min_confidence:.0%}")
        return None

    wave_dir = wave_5m.direction  # "up" | "down"

    # ─── 3. 다중 TF 방향 일치 점수 계산 ─────────────────────────
    align_score = 0
    for trend, weight in zip([trend_4h, trend_1h, trend_15m], [3, 2, 1]):
        if trend == wave_dir:
            align_score += weight
        elif trend != "sideways":
            align_score -= weight

    # 4h가 명확히 반대 방향이면 스킵 (4h 횡보는 허용)
    if trend_4h not in ("sideways",) and trend_4h != wave_dir:
        logger.info(f"[{symbol}] 4h 추세({trend_4h}) ↔ 5m 방향({wave_dir}) 반대 → 스킵")
        return None

    # 1h가 명확히 반대 방향이고 신뢰도가 낮으면 스킵
    if trend_1h not in ("sideways",) and trend_1h != wave_dir and wave_5m.confidence < R.counter_trend_min_conf:
        logger.info(f"[{symbol}] 1h 반추세({trend_1h}) + 신뢰도({wave_5m.confidence:.0%}) 부족 → 스킵")
        return None

    # 다중TF 정렬 점수: 음수(강한 역추세)면 스킵
    if align_score < -1:
        logger.info(f"[{symbol}] 다중TF 정렬 점수 부족: {align_score}/6")
        return None
    logger.info(f"[{symbol}] TF정렬 OK | score={align_score} | 5m={wave_5m.current_position} conf={wave_5m.confidence:.0%}")

    # ─── 4. 1m 정밀 타점 분석 ───────────────────────────────────
    wave_1m = analyze_wave(all_tf[TF_1M], pivot_left=3, pivot_right=3)
    # 1m 파동이 5m 방향과 일치하거나 진입 구간이면 신뢰도 보너스
    one_min_bonus = (wave_1m.entry_zone and wave_1m.direction == wave_dir)

    # ─── 5. RSI 다이버전스 분석 (5m 기준) ───────────────────────
    rsi_div     = detect_rsi_divergence(all_tf[TF_5M])
    div_aligned, _ = divergence_aligns_with_signal(rsi_div, wave_dir)
    rsi_confirmed   = rsi_div.detected and div_aligned

    # 다이버전스가 반대 방향이면 신뢰도 조건 강화
    if rsi_div.detected and not div_aligned and wave_5m.confidence < R.counter_trend_min_conf:
        logger.info(f"[{symbol}] RSI 다이버전스 역방향 + 신뢰도({wave_5m.confidence:.0%}) 부족 → 스킵")
        return None

    # ─── 6. PRG 확인 (현재가가 피보나치 중첩 구간 내인지) ────────
    prg = wave_5m.prg
    entry_price_raw = float(all_tf[TF_1M]["close"].iloc[-1])  # 1m 현재가
    prg_confirmed = is_price_in_prg(entry_price_raw, prg)

    # ─── 7. 동적 레버리지 결정 ───────────────────────────────────
    leverage = decide_leverage(prg_confirmed, rsi_confirmed, wave_5m.confidence)

    # ─── 8. 진입가 (호가 양보 적용) ─────────────────────────────
    side = "long" if wave_dir == "up" else "short"
    entry_price = apply_slippage_buffer(entry_price_raw, side)

    # ─── 9. 손절 / TP ────────────────────────────────────────────
    _sl_pct = R.stop_loss_pct
    if side == "long":
        stop_loss   = round(entry_price * (1 - _sl_pct), 6)
        take_profit = calculate_somonics_tp(entry_price, wave_5m.target_price, "long")
    else:
        stop_loss   = round(entry_price * (1 + _sl_pct), 6)
        take_profit = calculate_somonics_tp(entry_price, wave_5m.target_price, "short")

    # 손익비 확인 (최소 1:1.5)
    risk   = abs(entry_price - stop_loss)
    reward = abs(entry_price - take_profit)
    rr_ratio = reward / risk if risk > 0 else 0

    if rr_ratio < R.min_rr_ratio - 1e-9:
        logger.info(f"[{symbol}] 손익비 부족: 1:{rr_ratio:.2f} < 1:{R.min_rr_ratio}")
        return None

    # ─── 10. 최종 신뢰도 보정 ────────────────────────────────────
    final_conf = wave_5m.confidence
    if prg_confirmed:   final_conf = min(1.0, final_conf + 0.10)
    if rsi_confirmed:   final_conf = min(1.0, final_conf + 0.10)
    if one_min_bonus:   final_conf = min(1.0, final_conf + 0.05)

    reason = (
        f"[NEoWave+지침서] {wave_5m.wave_type.value} | {wave_5m.current_position.value} | "
        f"4h:{trend_4h} 1h:{trend_1h} 15m:{trend_15m} 5m:{wave_dir} | "
        f"신뢰:{final_conf:.0%} | PRG:{'O' if prg_confirmed else 'X'} | "
        f"RSI_DIV:{'O' if rsi_confirmed else 'X'} | "
        f"레버리지:{leverage}x | RR:{rr_ratio:.2f}"
    )

    entry_reason = _build_entry_reason(
        symbol, side, wave_5m, trend_4h, trend_1h, trend_15m,
        entry_price, stop_loss, take_profit, rr_ratio,
        leverage, prg, prg_confirmed, rsi_div, rsi_confirmed, align_score
    )

    signal = TradeSignal(
        symbol        = symbol,
        side          = side,
        entry_price   = round(entry_price, 6),
        stop_loss     = stop_loss,
        take_profit   = take_profit,
        confidence    = final_conf,
        leverage      = leverage,
        wave_position = wave_5m.current_position.value,
        wave_type     = wave_5m.wave_type.value,
        trend_4h      = trend_4h,
        trend_1h      = trend_1h,
        trend_15m     = trend_15m,
        wave_5m_dir   = wave_dir,
        reason        = reason,
        entry_reason  = entry_reason,
        rsi_confirmed = rsi_confirmed,
        prg_confirmed = prg_confirmed,
    )

    logger.info(
        f"[{symbol}] 신호! {side.upper()} {leverage}x | "
        f"진입:{entry_price:.4f} SL:{stop_loss:.4f} TP:{take_profit:.4f} | "
        f"RR:{rr_ratio:.2f} 신뢰:{final_conf:.0%}"
    )
    return signal


# ══════════════════════════════════════════════════════════════
# 진입 근거 텍스트 생성
# ══════════════════════════════════════════════════════════════

def _build_entry_reason(
    symbol, side, wave_5m, trend_4h, trend_1h, trend_15m,
    entry_price, stop_loss, take_profit, rr_ratio,
    leverage, prg, prg_confirmed, rsi_div, rsi_confirmed, align_score
) -> str:

    side_kor  = "롱(매수)" if side == "long" else "숏(매도)"
    pos       = wave_5m.current_position.value
    wtype     = wave_5m.wave_type.value
    conf      = wave_5m.confidence
    sl_pct    = abs(entry_price - stop_loss) / entry_price * 100
    tp_pct    = abs(entry_price - take_profit) / entry_price * 100

    trend_kor = {"up": "상승", "down": "하락", "sideways": "횡보"}

    # 파동 위치별 설명
    wave_descs = {
        "wave3": (
            "Wave 1~2 완성, Wave 3 진입 (가장 강력한 파동)\n"
            "  2파 되돌림이 1파의 38.2~61.8% 피보나치 지지 확인\n"
            "  절대법칙: 2파 61.8% 초과 없음 → 유효한 임펄스 구조"
        ),
        "wave5": (
            "Wave 1~4 완성, 마지막 Wave 5 진입\n"
            "  4파 조정이 1파 영역과 비겹침 확인\n"
            "  연장의 법칙: 가장 긴 파동이 두 번째의 1.618배 이상 검증"
        ),
        "wave_c": (
            f"ABC {'지그재그' if wtype == 'zigzag' else '플랫'} 조정 Wave C 진입\n"
            f"  {'B파가 A파의 61.8% 이하 되돌림 (지그재그 조건)' if wtype == 'zigzag' else 'B파가 A파의 61.8% 이상 되돌림 (플랫 조건)'}\n"
            f"  목표가: A파 크기 기준 0.618 되돌림 레벨 (소모닉 룰)"
        ),
    }
    wave_desc = wave_descs.get(pos, f"파동 위치 {pos}")

    # 다중TF 추세 정렬
    tf_lines = (
        f"  4h봉: {trend_kor.get(trend_4h, trend_4h)} {'✓' if trend_4h == wave_5m.direction else '─'}\n"
        f"  1h봉: {trend_kor.get(trend_1h, trend_1h)} {'✓' if trend_1h == wave_5m.direction else '─'}\n"
        f"  15m봉: {trend_kor.get(trend_15m, trend_15m)} {'✓' if trend_15m == wave_5m.direction else '─'}\n"
        f"  5m봉: 파동 분석 (메인 로직)\n"
        f"  1m봉: 정밀 타점 진입\n"
        f"  정렬 점수: {align_score}/6"
    )

    # PRG 섹션
    if prg_confirmed and prg.valid:
        prg_text = (
            f"  피보나치 중첩 확인! (PRG 유효)\n"
            f"  중첩 구간: {prg.price_low:.4f} ~ {prg.price_high:.4f}\n"
            f"  중첩 개수: {prg.confluence_count}개 피보나치 레벨"
        )
    else:
        prg_text = "  미감지 (파동 단독 신호)"

    # RSI 섹션
    if rsi_div and rsi_div.detected:
        strength_kor = {"strong": "강", "moderate": "중", "weak": "약"}.get(rsi_div.strength, "")
        confirm_mark = "확정 (신호 강화)" if rsi_confirmed else "불일치 (주의)"
        rsi_text = (
            f"  현재 RSI: {get_rsi_zone(rsi_div.rsi_value)}\n"
            f"  {rsi_div.description}\n"
            f"  강도: {strength_kor} | 판정: {confirm_mark}"
        )
    else:
        rsi_val = get_rsi_zone(rsi_div.rsi_value) if rsi_div else "N/A"
        rsi_text = f"  현재 RSI: {rsi_val}\n  다이버전스: 미감지"

    # 레버리지 근거
    if leverage >= R.leverage_max:
        lev_reason = "PRG + RSI 다이버전스 동시 확인 → 최고 확신 구간"
    elif leverage >= R.leverage_medium:
        lev_reason = "PRG 또는 RSI 다이버전스 단독 확인 → 중간 확신"
    else:
        lev_reason = "파동 단독 신호 → 기본 레버리지"

    lines = [
        f"[ {symbol} {side_kor} 진입 근거 ]",
        f"",
        f"  전략 : 글렌 닐리 NEoWave + 지침서 절대법칙 (단타)",
        f"  파동 : {wtype.upper()} | 위치: {pos.upper()}",
        f"",
        f"  [다중 타임프레임 추세]",
        tf_lines,
        f"",
        f"  [5m 파동 분석]",
        f"  {wave_desc}",
        f"",
        f"  [PRG (피보나치 중첩 반전 구간)]",
        prg_text,
        f"",
        f"  [RSI 다이버전스 (5m)]",
        rsi_text,
        f"",
        f"  [신호 품질]",
        f"  절대법칙 통과  : O",
        f"  NEoWave 신뢰도 : {conf:.0%}",
        f"  PRG 확인       : {'O' if prg_confirmed else 'X'}",
        f"  RSI 다이버전스 : {'O' if rsi_confirmed else 'X'}",
        f"",
        f"  [레버리지 결정]  {leverage}x",
        f"  사유: {lev_reason}",
        f"",
        f"  [가격 정보]",
        f"  진입가 : {entry_price:.4f} USDT  (호가양보 적용)",
        f"  손절가 : {stop_loss:.4f} USDT  (-{sl_pct:.1f}%)",
        f"  목표가 : {take_profit:.4f} USDT (+{tp_pct:.1f}%)  [소모닉 0.618 기준]",
        f"  손익비 : 1 : {rr_ratio:.2f}",
        f"",
        f"  [리스크]",
        f"  레버리지 {leverage}배 | 계좌 2% 손실 기준 수량 역산",
        f"  손절 -2% 히트 시 자동 청산 (포지션 홀딩 금지)",
    ]
    return "\n".join(lines)
