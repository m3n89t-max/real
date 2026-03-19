"""
바이낸스 선물 NEoWave + 지침서 자동매매 봇 v3.1

v3.1 개선:
- TP1 체결 감지 → SL 수량 자동 조정 (잔여 수량만 SL 유지)
- TP2 거래소 주문 배치 (봇 꺼져도 안전)
- 시드 100% 활용 (수익 포함 전액 재투입)
"""

import time
import logging
import sys
import threading
from datetime import datetime
from collections import defaultdict

from config import SYMBOLS, LOG_FILE
from rule_loader import R
from data_fetcher import (
    initialize_exchange, fetch_all_timeframes,
    get_account_balance, get_open_positions, set_leverage_dynamic,
    get_current_price,
)
from signal_generator import generate_signal, TradeSignal
from trader import (
    execute_trade, check_trailing_stop, update_stop_loss,
    adjust_sl_after_tp1,
)
from journal import print_stats, get_open_trades, update_stats, record_exit


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
║   바이낸스 선물 NEoWave 자동매매 봇 v3.1                    ║
║   전략: 글렌 닐리 파동 + 지침서 절대법칙 + PRG              ║
║   TF : 4h→1h→15m→5m(메인)→1m(타점)                        ║
║   레버리지: 3x / 5x / 7x (PRG+RSI 조건부)                  ║
║   시드: 100% 활용 | 수익 전액 재투입                         ║
║   TP1(50%) → SL조정 → 트레일링/TP2(50%) 자동              ║
║   대상: BTC ETH BNB SOL XRP DOGE LINK ARB                   ║
╚══════════════════════════════════════════════════════════════╝
""")


# ══════════════════════════════════════════════════════════════
# 쿨다운 / 손실 추적 매니저
# ══════════════════════════════════════════════════════════════

class CooldownManager:
    def __init__(self):
        self.cooldowns: dict[str, float] = {}
        self.consecutive_losses: dict[str, int] = defaultdict(int)
        self._lock = threading.Lock()

    def is_cooled_down(self, symbol: str) -> bool:
        with self._lock:
            if symbol in self.cooldowns:
                if time.time() < self.cooldowns[symbol]:
                    return False
                del self.cooldowns[symbol]
        return True

    def set_cooldown(self, symbol: str, seconds: int):
        with self._lock:
            self.cooldowns[symbol] = time.time() + seconds

    def record_loss(self, symbol: str):
        with self._lock:
            self.consecutive_losses[symbol] += 1
            if self.consecutive_losses[symbol] >= R.max_consecutive_losses:
                self.cooldowns[symbol] = time.time() + 3600
                logging.getLogger("cooldown").warning(
                    f"[{symbol}] 연속 {self.consecutive_losses[symbol]}회 손실 → 1시간 쿨다운"
                )

    def record_win(self, symbol: str):
        with self._lock:
            self.consecutive_losses[symbol] = 0

    def get_remaining(self, symbol: str) -> int:
        with self._lock:
            if symbol in self.cooldowns:
                return max(0, int(self.cooldowns[symbol] - time.time()))
        return 0


cooldown_mgr = CooldownManager()


# ══════════════════════════════════════════════════════════════
# 포지션 모니터링 (트레일링 + TP1 감지)
# ══════════════════════════════════════════════════════════════

class PositionTracker:
    def __init__(self):
        self.positions: dict[str, dict] = {}
        self._lock = threading.Lock()

    def add(self, symbol: str, side: str, entry_price: float,
            stop_loss: float, quantity: float, tp1_qty: float,
            tp2_qty: float, atr_pct: float, trade_id: str):
        with self._lock:
            self.positions[symbol] = {
                "side": side,
                "entry_price": entry_price,
                "original_sl": stop_loss,
                "current_sl": stop_loss,
                "quantity": quantity,
                "tp1_qty": tp1_qty,
                "tp2_qty": tp2_qty,
                "remaining_qty": quantity,
                "tp1_filled": False,
                "atr_pct": atr_pct,
                "trade_id": trade_id,
            }

    def remove(self, symbol: str):
        with self._lock:
            self.positions.pop(symbol, None)

    def get_all(self) -> dict:
        with self._lock:
            return {k: dict(v) for k, v in self.positions.items()}

    def update_sl(self, symbol: str, new_sl: float):
        with self._lock:
            if symbol in self.positions:
                self.positions[symbol]["current_sl"] = new_sl

    def mark_tp1_filled(self, symbol: str):
        with self._lock:
            if symbol in self.positions:
                p = self.positions[symbol]
                p["tp1_filled"] = True
                p["remaining_qty"] = p["tp2_qty"]

    def update_remaining_qty(self, symbol: str, qty: float):
        with self._lock:
            if symbol in self.positions:
                self.positions[symbol]["remaining_qty"] = qty


pos_tracker = PositionTracker()


def monitor_positions(exchange, logger):
    """오픈 포지션 모니터링: TP1 감지 + 트레일링 스탑"""
    tracked = pos_tracker.get_all()
    if not tracked:
        return

    for symbol, info in tracked.items():
        try:
            current_price = get_current_price(exchange, symbol)
            if current_price <= 0:
                continue

            # ─── TP1 체결 감지 ─────────────────────────────────
            if not info["tp1_filled"] and info["tp2_qty"] > 0:
                actual_pos = _get_position_qty(exchange, symbol)
                if actual_pos > 0 and actual_pos <= info["tp2_qty"] * 1.05:
                    logger.info(
                        f"[{symbol}] TP1 체결 감지! "
                        f"원래:{info['quantity']} → 잔여:{actual_pos}"
                    )
                    pos_tracker.mark_tp1_filled(symbol)

                    adjust_sl_after_tp1(
                        exchange, symbol, info["side"],
                        actual_pos, info["current_sl"]
                    )
                    pos_tracker.update_remaining_qty(symbol, actual_pos)
                    cooldown_mgr.record_win(symbol)

            # ─── 트레일링 스탑 ─────────────────────────────────
            remaining = info["remaining_qty"]
            if remaining <= 0:
                continue

            result = check_trailing_stop(
                exchange, symbol, info["side"],
                info["entry_price"], current_price,
                info["current_sl"], info["atr_pct"],
                remaining,
            )

            if result["action"] == "move_sl":
                old_sl = info["current_sl"]
                new_sl = result["new_sl"]
                if update_stop_loss(exchange, symbol, info["side"],
                                    remaining, old_sl, new_sl):
                    pos_tracker.update_sl(symbol, new_sl)
                    logger.info(f"[{symbol}] {result['reason']}")

        except Exception as e:
            logger.error(f"[{symbol}] 모니터링 오류: {e}")


def _get_position_qty(exchange, symbol: str) -> float:
    """거래소에서 실제 포지션 수량 조회"""
    try:
        positions = exchange.fetch_positions([symbol])
        for p in positions:
            if float(p.get("contracts", 0)) != 0:
                return abs(float(p["contracts"]))
    except Exception:
        pass
    return 0.0


def _get_last_fill_price(exchange, symbol):
    """해당 심볼의 가장 최근 체결가 조회"""
    try:
        trades = exchange.fetch_my_trades(symbol, limit=10)
        if trades:
            trades.sort(key=lambda t: t["timestamp"], reverse=True)
            return float(trades[0]["price"])
    except Exception:
        pass
    try:
        return get_current_price(exchange, symbol)
    except Exception:
        return None


def sync_positions(exchange, logger):
    """거래소 실제 포지션과 트래커 동기화 + 저널 청산 기록"""
    try:
        open_pos = get_open_positions(exchange)
        open_symbols = {p["symbol"].split(":")[0] for p in open_pos}

        tracked = pos_tracker.get_all()
        for symbol in list(tracked.keys()):
            if symbol not in open_symbols:
                info     = tracked[symbol]
                trade_id = info.get("trade_id")
                side     = info.get("side", "long")
                logger.info(f"[{symbol}] 포지션 청산 감지 → 저널 기록")

                exit_price = _get_last_fill_price(exchange, symbol)

                cur_sl  = info.get("current_sl", 0)
                orig_sl = info.get("original_sl", 0)
                if cur_sl > orig_sl:
                    exit_reason = "TP"
                    cooldown_mgr.record_win(symbol)
                elif exit_price:
                    ep = info.get("entry_price", exit_price)
                    if side == "long":
                        exit_reason = "TP" if exit_price > ep else "SL"
                    else:
                        exit_reason = "TP" if exit_price < ep else "SL"
                    if exit_reason == "TP":
                        cooldown_mgr.record_win(symbol)
                    else:
                        cooldown_mgr.record_loss(symbol)
                        cooldown_mgr.set_cooldown(symbol, R.loss_cooldown_sec)
                else:
                    exit_reason = "SL"
                    cooldown_mgr.record_loss(symbol)
                    cooldown_mgr.set_cooldown(symbol, R.loss_cooldown_sec)

                if trade_id and exit_price:
                    try:
                        record_exit(trade_id, exit_price, exit_reason)
                        logger.info(f"[{symbol}] 저널 동기화: {exit_reason} @ {exit_price:.4f}")
                    except Exception as je:
                        logger.error(f"[{symbol}] 저널 청산 기록 실패: {je}")

                pos_tracker.remove(symbol)
    except Exception as e:
        logger.error(f"포지션 동기화 오류: {e}")


# ══════════════════════════════════════════════════════════════
# 스캔 루프
# ══════════════════════════════════════════════════════════════

def scan_all_symbols(exchange) -> list[TradeSignal]:
    logger = logging.getLogger("scanner")
    signals = []

    logger.info(f"{'─'*60}")
    logger.info(f"스캔: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    for symbol in SYMBOLS:
        if not cooldown_mgr.is_cooled_down(symbol):
            remaining = cooldown_mgr.get_remaining(symbol)
            logger.info(f"[{symbol}] 쿨다운 ({remaining}초 남음) → 스킵")
            continue

        try:
            all_tf = fetch_all_timeframes(exchange, symbol)
            if not all_tf:
                continue

            signal = generate_signal(symbol, all_tf)
            if signal:
                signals.append(signal)
                logger.info(
                    f"[{symbol}] 신호! {signal.side.upper()} {signal.leverage}x | "
                    f"신뢰:{signal.confidence:.0%} | ATR:{signal.atr_pct:.3%}"
                )

            time.sleep(0.5)

        except Exception as e:
            logger.error(f"[{symbol}] 스캔 오류: {e}")

    logger.info(f"스캔 완료 | 신호: {len(signals)}개")
    return signals


def _print_signal(signal: TradeSignal, logger):
    border = "=" * 62
    logger.info(f"\n{border}\n{signal.entry_reason}\n{border}")


def main():
    setup_logging()
    logger = logging.getLogger("main")
    print_banner()

    update_stats()
    print_stats()

    logger.info("거래소 초기화 중...")
    try:
        exchange = initialize_exchange()
        logger.info("초기화 완료")
    except Exception as e:
        logger.critical(f"초기화 실패: {e}")
        sys.exit(1)

    init_balance = get_account_balance(exchange)
    logger.info(f"초기잔고: {init_balance:.2f} USDT (전액 투입 가능)")

    open_journal = get_open_trades()
    if open_journal:
        logger.warning(f"[일지] 미청산 기록 {len(open_journal)}건 존재 (수동 확인 필요)")
        for t in open_journal:
            logger.warning(f"  -> {t['trade_id']} | {t['symbol']} {t['side']} | 진입: {t['entry_price']}")

    scan_count = 0

    while True:
        scan_count += 1
        logger.info(f"\n[스캔 #{scan_count}]")

        try:
            sync_positions(exchange, logger)
            monitor_positions(exchange, logger)

            signals = scan_all_symbols(exchange)
            signals.sort(key=lambda s: (s.leverage, s.confidence), reverse=True)

            balance = get_account_balance(exchange)
            open_pos = get_open_positions(exchange)

            if scan_count % 10 == 1:
                logger.info(f"잔고: {balance:.2f} USDT (전액 투입 가능)")

            for signal in signals:
                _print_signal(signal, logger)

                set_leverage_dynamic(exchange, signal.symbol, signal.leverage)

                result = execute_trade(
                    exchange, signal,
                    balance=balance,
                    open_positions=open_pos,
                    cooldown_tracker=cooldown_mgr.cooldowns,
                )
                if result:
                    logger.info(f"[{signal.symbol}] 주문 완료 ({signal.leverage}x)")
                    pos_tracker.add(
                        signal.symbol, signal.side,
                        result["actual_entry"], result["actual_sl"],
                        result["quantity"], result["tp1_qty"],
                        result["tp2_qty"], signal.atr_pct,
                        result["trade_id"],
                    )
                    cooldown_mgr.set_cooldown(signal.symbol, R.entry_cooldown_sec)
                    balance = get_account_balance(exchange)
                    open_pos = get_open_positions(exchange)
                else:
                    logger.info(f"[{signal.symbol}] 주문 스킵")

                time.sleep(1)

        except KeyboardInterrupt:
            logger.info("\nCtrl+C -> 봇 종료")
            break
        except Exception as e:
            logger.error(f"메인 루프 오류: {e}", exc_info=True)

        if R.check_and_reload():
            logger.info("지침서 파라미터 재로드 완료 — 다음 스캔부터 적용")

        interval = R.scan_interval_sec
        logger.info(f"{interval}초 후 재스캔...")
        time.sleep(interval)


if __name__ == "__main__":
    main()
