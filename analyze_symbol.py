"""
특정 코인 파동 분석 결과 확인 스크립트 (실거래 없음)
사용: python analyze_symbol.py XRP/USDT
      python analyze_symbol.py BTC/USDT ETH/USDT
"""

import sys
import logging
logging.basicConfig(level=logging.WARNING)

from data_fetcher import create_exchange, fetch_all_timeframes
from signal_generator import generate_signal
from wave_analyzer import analyze_wave, get_trend_direction, detect_pivots
from indicators import detect_rsi_divergence, get_rsi_zone, calculate_rsi
from config import TF_4H, TF_1H, TF_15M, TF_5M, TF_1M


def analyze(symbol: str):
    print(f"\n{'='*65}")
    print(f"  {symbol} 전체 타임프레임 분석 리포트")
    print(f"{'='*65}")

    exchange = create_exchange()
    exchange.load_markets()

    all_tf = fetch_all_timeframes(exchange, symbol)
    if not all_tf:
        print("  데이터 수집 실패")
        return

    # 각 TF 추세
    print(f"\n  [다중 타임프레임 추세]")
    for tf in [TF_4H, TF_1H, TF_15M]:
        trend = get_trend_direction(all_tf[tf])
        trend_kor = {"up": "상승", "down": "하락", "sideways": "횡보"}.get(trend, trend)
        pivots = detect_pivots(all_tf[tf])
        print(f"  {tf:>4s}봉: {trend_kor} ({len(pivots)}개 피벗 감지)")

    # 5m 파동 분석 (메인 로직)
    wave_5m = analyze_wave(all_tf[TF_5M])
    print(f"\n  [5m 파동 분석 - 메인 로직]")
    print(f"  파동 유형    : {wave_5m.wave_type.value}")
    print(f"  현재 위치    : {wave_5m.current_position.value}")
    print(f"  진입 구간    : {'예' if wave_5m.entry_zone else '아니오'}")
    print(f"  절대법칙 통과: {'O' if wave_5m.abs_law_passed else 'X'}")
    if wave_5m.rejection_reason:
        print(f"  기각 사유    : {wave_5m.rejection_reason}")
    print(f"  파동 방향    : {wave_5m.direction}")
    print(f"  신뢰도       : {wave_5m.confidence:.0%}")
    if wave_5m.target_price:
        print(f"  파동 목표가  : {wave_5m.target_price:.4f}")
    if wave_5m.invalidation_price:
        print(f"  무효화 가격  : {wave_5m.invalidation_price:.4f}")

    # PRG
    prg = wave_5m.prg
    print(f"\n  [PRG - 피보나치 중첩 반전 구간]")
    if prg.valid:
        print(f"  유효: O | 구간: {prg.price_low:.4f} ~ {prg.price_high:.4f}")
        print(f"  중첩 개수: {prg.confluence_count}개")
    else:
        print("  미감지")

    # 1m 타점
    wave_1m = analyze_wave(all_tf[TF_1M], pivot_left=3, pivot_right=3)
    print(f"\n  [1m 정밀 타점]")
    print(f"  1m 진입구간: {'예' if wave_1m.entry_zone else '아니오'} | 방향: {wave_1m.direction}")

    # RSI 다이버전스 (5m)
    rsi_div = detect_rsi_divergence(all_tf[TF_5M])
    current_rsi = calculate_rsi(all_tf[TF_5M]).iloc[-1]
    print(f"\n  [RSI 다이버전스 (5m)]")
    print(f"  현재 RSI: {get_rsi_zone(current_rsi)}")
    if rsi_div.detected:
        print(f"  다이버전스: {rsi_div.description}")
        print(f"  강도: {rsi_div.strength} | 신호방향: {rsi_div.signal_direction}")
    else:
        print("  다이버전스: 미감지")

    # 추세선
    tl = wave_5m.trendline
    print(f"\n  [추세선 분석]")
    if tl.has_24_line:
        print(f"  2-4 추세선: {tl.line_24_price:.4f} | 돌파: {'O (임펄스 마감)' if tl.broken_24 else 'X'}")
    if tl.has_bd_line:
        print(f"  B-D 추세선: {tl.line_bd_price:.4f} | 돌파: {'O (삼각형 분출)' if tl.broken_bd else 'X'}")

    # 최종 신호
    signal = generate_signal(symbol, all_tf)
    print(f"\n  [최종 매매 신호]")
    if signal:
        print(signal.entry_reason)
    else:
        print("  현재 진입 신호 없음")
        print(f"  (5m 진입구간={wave_5m.entry_zone}, 절대법칙={wave_5m.abs_law_passed}, "
              f"신뢰도={wave_5m.confidence:.0%})")

    print(f"\n{'='*65}\n")


if __name__ == "__main__":
    symbols = sys.argv[1:] if len(sys.argv) > 1 else ["XRP/USDT"]
    for sym in symbols:
        analyze(sym.upper())
