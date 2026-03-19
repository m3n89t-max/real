"""
5개 타임프레임 계층 구조 매매 신호 생성 — 초단타(스캘핑) 모드

계층 구조:
  4h  → 거시 추세 방향 (Big Degree, 진입 방향 필터)
  1h  → 중기 추세 확인 (반추세 차단)
  15m → 세부 파동 방향 필터
  5m  → 진입 방향 최종 확인 (추세 정렬 가중치)
  1m  → 메인 파동 분석 + PRG + 진입 타점 (스캘핑 핵심)

레버리지 결정:
  기본  3x  : 파동 단독 신호
  중간  5x  : PRG 또는 RSI 다이버전스 단독 확인
  최대  7x  : PRG + RSI 다이버전스 동시 확인
"""

from dataclasses import dataclass, field
from typing import Optional
import logging
from wave_analyzer import (
    analyze_wave, get_trend_direction,
    WavePosition, WaveType, is_price_in_prg,
    _determine_extension_type,
)
from indicators import (
    detect_rsi_divergence, get_rsi_zone,
    divergence_aligns_with_signal, DivergenceResult,
    get_atr_pct, get_atr_stop_distance,
    is_volume_confirmed, get_ema_trend,
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
    leverage: int               # 동적 레버리지 (3/5/7)
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
    atr_pct: float = 0.0       # ATR% (트레일링용)


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
    5개 TF 계층 분석으로 매매 신호 생성 — 초단타(스캘핑) 모드

    Args:
        symbol : 코인 심볼
        all_tf : {"4h": df, "1h": df, "15m": df, "5m": df, "1m": df}

    계층:
        4h/1h/15m → 거시 추세 방향 필터 (진입 차단/허용)
        5m        → 방향 정렬 확인 (추가 가중치)
        1m        → 메인 파동 분석 + PRG + 진입 타점 (스캘핑 핵심)
    """
    required = [TF_4H, TF_1H, TF_15M, TF_5M, TF_1M]
    if not all_tf or any(tf not in all_tf for tf in required):
        return None

    # ─── 1. 거시 추세 분석 (4h → 1h → 15m → 5m) ─────────────────
    trend_4h  = get_trend_direction(all_tf[TF_4H])
    trend_1h  = get_trend_direction(all_tf[TF_1H])
    trend_15m = get_trend_direction(all_tf[TF_15M])
    trend_5m  = get_trend_direction(all_tf[TF_5M])

    logger.info(f"[{symbol}] 추세: 4h={trend_4h} | 1h={trend_1h} | 15m={trend_15m} | 5m={trend_5m}")

    # 4h + 1h + 15m + 5m 모두 횡보면 스킵
    if (trend_4h == "sideways" and trend_1h == "sideways"
            and trend_15m == "sideways" and trend_5m == "sideways"):
        logger.info(f"[{symbol}] 전 타임프레임 횡보 → 스킵")
        return None

    # ─── 2. 1m 메인 파동 분석 (스캘핑 핵심) ───────────────────────
    wave_1m = analyze_wave(all_tf[TF_1M], pivot_left=3, pivot_right=3)

    if not wave_1m.entry_zone:
        logger.info(f"[{symbol}] 1m 진입 구간 아님 (위치:{wave_1m.current_position})")
        return None

    if not wave_1m.abs_law_passed:
        logger.info(f"[{symbol}] 1m 절대법칙 미통과: {wave_1m.rejection_reason}")
        return None

    if wave_1m.confidence < R.min_confidence:
        logger.info(f"[{symbol}] 1m 신뢰도 부족: {wave_1m.confidence:.0%} < {R.min_confidence:.0%}")
        return None

    wave_dir = wave_1m.direction  # "up" | "down"

    # ─── 3. 다중 TF 방향 일치 점수 계산 ─────────────────────────
    # 4h(3) > 1h(2) > 15m(1) > 5m(1) 가중치
    align_score = 0
    for trend, weight in zip([trend_4h, trend_1h, trend_15m, trend_5m], [3, 2, 1, 1]):
        if trend == wave_dir:
            align_score += weight
        elif trend != "sideways":
            align_score -= weight

    # ── 필터2: 롱/숏 방향별 TF 조건 ─────────────────────────────
    if wave_dir == "up":   # ── LONG 조건 ──
        # 4h가 하락이면 매크로 역행 → 금지
        if trend_4h == "down":
            logger.info(f"[{symbol}] [필터2-롱] 4h({trend_4h}) 하락 중 → 스킵")
            return None
        # 1h도 하락이면 중기 역행 → 금지
        if trend_1h == "down":
            logger.info(f"[{symbol}] [필터2-롱] 1h({trend_1h}) 하락 중 → 스킵")
            return None
        # 4h·1h 모두 횡보면 15m 상승 필요
        if trend_4h == "sideways" and trend_1h == "sideways" and trend_15m != "up":
            logger.info(f"[{symbol}] [필터2-롱] 4h·1h 횡보 + 15m({trend_15m}) 미확인 → 스킵")
            return None

    else:                  # ── SHORT 조건 ──
        # 4h가 상승이어도 숏 허용 (스캘핑 반전 포착)
        # 단, 1h는 반드시 하락 또는 횡보여야 함
        if trend_1h == "up":
            logger.info(f"[{symbol}] [필터2-숏] 1h({trend_1h}) 상승 중 → 스킵")
            return None
        # 1h 횡보면 15m 하락 필요
        if trend_1h == "sideways" and trend_15m != "down":
            logger.info(f"[{symbol}] [필터2-숏] 1h 횡보 + 15m({trend_15m}) 미확인 → 스킵")
            return None

    # 다중TF 정렬 점수가 너무 낮으면 스킵
    if align_score < -1:
        logger.info(f"[{symbol}] 다중TF 정렬 점수 부족: {align_score}/7")
        return None
    logger.info(
        f"[{symbol}] TF정렬 OK | score={align_score}/7 | "
        f"1m={wave_1m.current_position} conf={wave_1m.confidence:.0%}"
    )

    # ─── 3.5. 볼륨 필터 (1m 기준) ───────────────────────────────
    if not is_volume_confirmed(all_tf[TF_1M], threshold=R.volume_threshold):
        logger.info(f"[{symbol}] 1m 거래량 부족 → 스킵")
        return None

    # ─── 3.6. EMA 추세 정렬 필터 (5m 기준) ────────────────────
    if R.require_ema_align:
        ema_dir = get_ema_trend(all_tf[TF_5M])
        if ema_dir != "sideways" and ema_dir != wave_dir:
            logger.info(f"[{symbol}] EMA 추세({ema_dir}) ↔ 1m 방향({wave_dir}) 불일치 → 스킵")
            return None

    # ─── 4. RSI 다이버전스 분석 (1m 기준) ───────────────────────
    rsi_div     = detect_rsi_divergence(all_tf[TF_1M])
    div_aligned, _ = divergence_aligns_with_signal(rsi_div, wave_dir)
    rsi_confirmed   = rsi_div.detected and div_aligned

    # 다이버전스 역방향 + 신뢰도 부족 → 스킵
    if rsi_div.detected and not div_aligned and wave_1m.confidence < R.counter_trend_min_conf:
        logger.info(f"[{symbol}] RSI 다이버전스 역방향 + 신뢰도({wave_1m.confidence:.0%}) 부족 → 스킵")
        return None

    # ─── 5. PRG 확인 (1m 현재가 기준) ───────────────────────────
    prg = wave_1m.prg
    entry_price_raw = float(all_tf[TF_1M]["close"].iloc[-1])
    prg_confirmed = is_price_in_prg(entry_price_raw, prg)

    # ─── 6. 동적 레버리지 결정 ───────────────────────────────────
    leverage = decide_leverage(prg_confirmed, rsi_confirmed, wave_1m.confidence)

    # ─── 7. 진입가 (호가 양보 적용) ─────────────────────────────
    side = "long" if wave_dir == "up" else "short"
    entry_price = apply_slippage_buffer(entry_price_raw, side)

    # ─── 8. ATR 기반 동적 손절 / TP (1m 기준 — 타이트하게) ──────
    atr_pct = get_atr_pct(all_tf[TF_1M])
    atr_sl_pct = atr_pct * R.atr_sl_multiplier
    atr_sl_pct = max(R.atr_sl_min_pct, min(R.atr_sl_max_pct, atr_sl_pct))
    _sl_pct = max(atr_sl_pct, R.stop_loss_pct)

    logger.info(f"[{symbol}] ATR%(1m)={atr_pct:.3%} → 동적SL={_sl_pct:.3%}")

    if side == "long":
        stop_loss   = round(entry_price * (1 - _sl_pct), 6)
        take_profit = calculate_somonics_tp(entry_price, wave_1m.target_price, "long")
    else:
        stop_loss   = round(entry_price * (1 + _sl_pct), 6)
        take_profit = calculate_somonics_tp(entry_price, wave_1m.target_price, "short")

    # 손익비 확인
    risk     = abs(entry_price - stop_loss)
    reward   = abs(entry_price - take_profit)
    rr_ratio = reward / risk if risk > 0 else 0

    if rr_ratio < R.min_rr_ratio - 1e-9:
        logger.info(f"[{symbol}] 손익비 부족: 1:{rr_ratio:.2f} < 1:{R.min_rr_ratio}")
        return None

    # ─── 9. 최종 신뢰도 보정 ─────────────────────────────────────
    final_conf = wave_1m.confidence
    if prg_confirmed:           final_conf = min(1.0, final_conf + 0.10)
    if rsi_confirmed:           final_conf = min(1.0, final_conf + 0.10)
    if trend_5m == wave_dir:    final_conf = min(1.0, final_conf + 0.05)  # 5m 정렬 보너스

    reason = (
        f"[NEoWave 스캘핑] {wave_1m.wave_type.value} | {wave_1m.current_position.value} | "
        f"4h:{trend_4h} 1h:{trend_1h} 15m:{trend_15m} 5m:{trend_5m} 1m:{wave_dir} | "
        f"신뢰:{final_conf:.0%} | PRG:{'O' if prg_confirmed else 'X'} | "
        f"RSI_DIV:{'O' if rsi_confirmed else 'X'} | "
        f"레버리지:{leverage}x | RR:{rr_ratio:.2f}"
    )

    entry_reason = _build_entry_reason(
        symbol, side, wave_1m, trend_4h, trend_1h, trend_15m,
        entry_price, stop_loss, take_profit, rr_ratio,
        leverage, prg, prg_confirmed, rsi_div, rsi_confirmed, align_score,
        trend_5m=trend_5m,
    )

    signal = TradeSignal(
        symbol        = symbol,
        side          = side,
        entry_price   = round(entry_price, 6),
        stop_loss     = stop_loss,
        take_profit   = take_profit,
        confidence    = final_conf,
        leverage      = leverage,
        wave_position = wave_1m.current_position.value,
        wave_type     = wave_1m.wave_type.value,
        trend_4h      = trend_4h,
        trend_1h      = trend_1h,
        trend_15m     = trend_15m,
        wave_5m_dir   = trend_5m,
        reason        = reason,
        entry_reason  = entry_reason,
        rsi_confirmed = rsi_confirmed,
        prg_confirmed = prg_confirmed,
        atr_pct       = atr_pct,
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
    symbol, side, wave_1m, trend_4h, trend_1h, trend_15m,
    entry_price, stop_loss, take_profit, rr_ratio,
    leverage, prg, prg_confirmed, rsi_div, rsi_confirmed, align_score,
    trend_5m: str = "sideways",
) -> str:

    side_kor  = "롱(매수)" if side == "long" else "숏(매도)"
    pos       = wave_1m.current_position.value
    wtype     = wave_1m.wave_type.value
    conf      = wave_1m.confidence
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
            f"  목표가: A파 크기 기준 0.618~1.0 되돌림 레벨"
        ),
        "complete": (
            "삼각형 수렴 완성 → B-D 추세선 돌파 분출 진입\n"
            "  3-3-3-3-3 구조 확인 (최소 3개 파동이 직전의 50% 이상 되돌림)\n"
            "  목표가: 삼각형 내 최대 파동 크기의 100% (75~125% 범위)"
        ),
    }

    # 터미널 충격파는 wave5 포지션이지만 wave_type으로 구분
    if wtype == "terminal":
        wave_descs["wave5"] = (
            "터미널 충격파(다이아고날) 완성 → 강한 반전 진입\n"
            "  3-3-3-3-3 구조 + 2파-4파 가격대 겹침 확인\n"
            "  2-4 추세선 돌파 → 터미널 마감 확정\n"
            "  목표가: 터미널 시작점 100% 되돌림 (지침서 모듈5)"
        )

    wave_desc = wave_descs.get(pos, f"파동 위치 {pos}")

    # 다중TF 추세 정렬
    d = wave_1m.direction
    tf_lines = (
        f"  4h봉: {trend_kor.get(trend_4h,  trend_4h)}  {'✓' if trend_4h  == d else '─'}\n"
        f"  1h봉: {trend_kor.get(trend_1h,  trend_1h)}  {'✓' if trend_1h  == d else '─'}\n"
        f"  15m봉: {trend_kor.get(trend_15m, trend_15m)} {'✓' if trend_15m == d else '─'}\n"
        f"  5m봉: {trend_kor.get(trend_5m,  trend_5m)}  {'✓' if trend_5m  == d else '─'}\n"
        f"  1m봉: 파동 분석 (스캘핑 메인)\n"
        f"  정렬 점수: {align_score}/7"
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
            f"  현재 RSI(1m): {get_rsi_zone(rsi_div.rsi_value)}\n"
            f"  {rsi_div.description}\n"
            f"  강도: {strength_kor} | 판정: {confirm_mark}"
        )
    else:
        rsi_val = get_rsi_zone(rsi_div.rsi_value) if rsi_div else "N/A"
        rsi_text = f"  현재 RSI(1m): {rsi_val}\n  다이버전스: 미감지"

    # 레버리지 근거
    if wtype == "terminal":
        lev_reason = f"터미널 충격파 반전 신호 + {'PRG 확인' if prg_confirmed else '파동 단독'}"
    elif wtype == "triangle":
        lev_reason = f"삼각형 B-D 돌파 신호 + {'PRG 확인' if prg_confirmed else '파동 단독'}"
    elif leverage >= R.leverage_max:
        lev_reason = "PRG + RSI 다이버전스 동시 확인 → 최고 확신 구간"
    elif leverage >= R.leverage_medium:
        lev_reason = "PRG 또는 RSI 다이버전스 단독 확인 → 중간 확신"
    else:
        lev_reason = "파동 단독 신호 → 기본 레버리지"

    lines = [
        f"[ {symbol} {side_kor} 진입 근거 ]",
        f"",
        f"  전략 : 글렌 닐리 NEoWave + 지침서 절대법칙 (초단타 스캘핑)",
        f"  파동 : {wtype.upper()} | 위치: {pos.upper()}",
        f"",
        f"  [다중 타임프레임 추세]",
        tf_lines,
        f"",
        f"  [1m 파동 분석 (스캘핑 메인)]",
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
        f"  목표가 : {take_profit:.4f} USDT (+{tp_pct:.1f}%)  "
        f"[{'터미널 시작점 100% 되돌림' if wtype == 'terminal' else '삼각형 최대파동 100%' if wtype == 'triangle' else '소모닉 0.618 기준'}]",
        f"  손익비 : 1 : {rr_ratio:.2f}",
        f"",
        f"  [리스크]",
        f"  레버리지 {leverage}배 | 계좌 2% 손실 기준 수량 역산",
        f"  손절 -2% 히트 시 자동 청산 (포지션 홀딩 금지)",
    ]
    return "\n".join(lines)
