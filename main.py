"""
바이낸스 선물 NEoWave + 지침서 자동매매 봇 v2.0

타임프레임 계층: 4h → 1h → 15m → 5m(메인) → 1m(타점)
레버리지: 4x(기본) / 7x(PRG 또는 RSI) / 10x(PRG + RSI 동시)
손실한도: 개별 포지션당 계좌 2%
"""

import time
import logging
import sys
from datetime import datetime

from config import SYMBOLS, LOG_FILE
from rule_loader import R
from data_fetcher import (
    initialize_exchange, fetch_all_timeframes,
    get_account_balance, get_open_positions, set_leverage_dynamic
)
from signal_generator import generate_signal, TradeSignal
from trader import execute_trade
from journal import print_stats, get_open_trades


def setup_logging():
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
        ]
    )
    for noisy in ["ccxt", "urllib3", "asyncio"]:
        logging.getLogger(noisy).setLevel(logging.WARNING)


def print_banner():
    print("""
╔══════════════════════════════════════════════════════════════╗
║   바이낸스 선물 NEoWave 자동매매 봇 v2.0                    ║
║   전략: 글렌 닐리 파동 + 지침서 절대법칙 + PRG              ║
║   TF : 4h→1h→15m→5m(메인)→1m(타점)                        ║
║   레버리지: 4x / 7x / 10x (PRG+RSI 조건부)                  ║
║   손실한도: 포지션당 계좌 2%                                 ║
║   대상: BTC ETH BNB SOL XRP DOGE LINK ARB                   ║
╚══════════════════════════════════════════════════════════════╝
""")


def scan_all_symbols(exchange) -> list[TradeSignal]:
    """8개 코인 순차 스캔, 신호 반환"""
    logger = logging.getLogger("scanner")
    signals = []

    logger.info(f"{'─'*60}")
    logger.info(f"스캔: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    for symbol in SYMBOLS:
        try:
            all_tf = fetch_all_timeframes(exchange, symbol)
            if not all_tf:
                continue

            signal = generate_signal(symbol, all_tf)
            if signal:
                signals.append(signal)
                logger.info(
                    f"[{symbol}] 신호! {signal.side.upper()} {signal.leverage}x | "
                    f"신뢰:{signal.confidence:.0%}"
                )

            time.sleep(0.5)   # Rate limit 방지

        except Exception as e:
            logger.error(f"[{symbol}] 스캔 오류: {e}")

    logger.info(f"스캔 완료 | 신호: {len(signals)}개")
    return signals


def _print_signal(signal: TradeSignal, logger):
    """매매 신호 상세 출력"""
    border = "=" * 62
    logger.info(f"\n{border}\n{signal.entry_reason}\n{border}")


def main():
    setup_logging()
    logger = logging.getLogger("main")
    print_banner()

    # ─── 매매일지 통계 출력 ──────────────────────────────────────
    print_stats()

    logger.info("거래소 초기화 중...")
    try:
        exchange = initialize_exchange()
        logger.info("초기화 완료")
    except Exception as e:
        logger.critical(f"초기화 실패: {e}")
        sys.exit(1)

    # 미청산 오픈 기록 경고
    open_journal = get_open_trades()
    if open_journal:
        logger.warning(f"[일지] 미청산 기록 {len(open_journal)}건 존재 (수동 확인 필요)")
        for t in open_journal:
            logger.warning(f"  → {t['trade_id']} | {t['symbol']} {t['side']} | 진입: {t['entry_price']}")

    scan_count = 0

    while True:
        scan_count += 1
        logger.info(f"\n[스캔 #{scan_count}]")

        try:
            signals = scan_all_symbols(exchange)

            # 신뢰도 + 레버리지 높은 순으로 우선 처리
            signals.sort(key=lambda s: (s.leverage, s.confidence), reverse=True)

            balance = get_account_balance(exchange)
            open_pos = get_open_positions(exchange)

            for signal in signals:
                _print_signal(signal, logger)

                # 진입 직전 레버리지 동적 설정
                set_leverage_dynamic(exchange, signal.symbol, signal.leverage)

                success = execute_trade(
                    exchange, signal,
                    balance=balance,
                    open_positions=open_pos,
                )
                if success:
                    logger.info(f"[{signal.symbol}] 주문 완료 ({signal.leverage}x)")
                    # 주문 후 포지션/잔고 갱신
                    balance  = get_account_balance(exchange)
                    open_pos = get_open_positions(exchange)
                else:
                    logger.info(f"[{signal.symbol}] 주문 스킵")

                time.sleep(1)

        except KeyboardInterrupt:
            logger.info("\nCtrl+C → 봇 종료")
            break
        except Exception as e:
            logger.error(f"메인 루프 오류: {e}", exc_info=True)

        # rules_config.json 변경 감지 → 파라미터 자동 재로드
        if R.check_and_reload():
            logger.info("📋 지침서 파라미터 재로드 완료 — 다음 스캔부터 적용")

        interval = R.scan_interval_sec
        logger.info(f"{interval}초 후 재스캔...")
        time.sleep(interval)


if __name__ == "__main__":
    main()
