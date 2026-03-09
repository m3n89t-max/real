"""
바이낸스 선물 실거래 주문 실행 모듈

주문 순서:
1. 시장가 진입
2. Stop Market (손절)
3. Take Profit Market (목표가)
"""

import time
import logging
from signal_generator import TradeSignal
from risk_manager import (
    calculate_position_size, round_quantity,
    is_risk_acceptable, get_market_precision
)
from journal import record_entry, record_exit
from config import MAX_OPEN_POSITIONS

logger = logging.getLogger(__name__)


def _place_order(exchange, symbol, side, order_type, quantity, price=None, params=None):
    """단일 주문 실행 래퍼"""
    params = params or {}
    try:
        order_side = "buy" if side == "long" else "sell"
        close_side = "sell" if side == "long" else "buy"

        if order_type == "market":
            order = exchange.create_order(symbol, "market", order_side, quantity, None, params)

        elif order_type == "stop_market":
            params["stopPrice"] = price
            order = exchange.create_order(symbol, "stop_market", close_side, quantity, None, params)

        elif order_type == "take_profit_market":
            params["stopPrice"] = price
            order = exchange.create_order(symbol, "take_profit_market", close_side, quantity, None, params)

        else:
            raise ValueError(f"알 수 없는 주문 타입: {order_type}")

        logger.info(f"[{symbol}] {order_type} {side} qty={quantity} price={price or 'MKT'}")
        return order

    except Exception as e:
        logger.error(f"[{symbol}] 주문 실패 ({order_type}): {e}")
        return {}


def execute_trade(
    exchange,
    signal: TradeSignal,
    balance: float = 0.0,
    open_positions: list = None,
) -> bool:
    """
    신호를 받아 실제 주문 실행

    Returns:
        True: 성공, False: 스킵/실패
    """
    symbol   = signal.symbol
    open_pos = open_positions or []
    open_count = len(open_pos)

    # ─── 1. 중복 포지션 확인 ─────────────────────────────────────
    existing = [
        p["symbol"].replace(":USDT", "").replace("USDT", "") + "/USDT"
        for p in open_pos
    ]
    if symbol in existing:
        logger.info(f"[{symbol}] 이미 포지션 존재 → 스킵")
        return False

    if open_count >= MAX_OPEN_POSITIONS:
        logger.warning(f"최대 포지션 수 ({open_count}/{MAX_OPEN_POSITIONS}) → 스킵")
        return False

    # ─── 2. 잔고 확인 ────────────────────────────────────────────
    if balance <= 0:
        logger.error(f"잔고 없음: {balance:.2f} USDT")
        return False

    # ─── 3. 수량 계산 (계좌 2% 역산) ─────────────────────────────
    precision = get_market_precision(exchange, symbol)
    quantity  = calculate_position_size(
        balance       = balance,
        entry_price   = signal.entry_price,
        stop_loss     = signal.stop_loss,
        leverage      = signal.leverage,
        open_position_count = open_count,
        symbol        = symbol,
    )

    if quantity <= 0:
        logger.warning(f"[{symbol}] 수량 계산 실패")
        return False

    # 거래소 최소 단위 조정
    quantity = round_quantity(quantity, precision.get("amount", 0.001))
    if quantity < precision.get("min_qty", 0.001):
        logger.warning(f"[{symbol}] 최소 수량 미달: {quantity}")
        return False

    # ─── 4. 리스크 최종 검증 ─────────────────────────────────────
    if not is_risk_acceptable(
        balance, signal.entry_price, signal.stop_loss,
        signal.take_profit, quantity, signal.leverage, symbol
    ):
        logger.warning(f"[{symbol}] 리스크 검증 실패 → 스킵")
        return False

    # ─── 5. 주문 실행 ────────────────────────────────────────────
    logger.info(
        f"\n{'='*55}\n"
        f"[{symbol}] 주문 실행\n"
        f"  방향    : {signal.side.upper()}\n"
        f"  레버리지: {signal.leverage}x\n"
        f"  진입가  : {signal.entry_price:.4f} USDT\n"
        f"  손절가  : {signal.stop_loss:.4f} USDT (-2%)\n"
        f"  목표가  : {signal.take_profit:.4f} USDT\n"
        f"  수량    : {quantity}\n"
        f"  잔고    : {balance:.2f} USDT\n"
        f"{'='*55}"
    )

    entry_order = _place_order(exchange, symbol, signal.side, "market", quantity)
    if not entry_order:
        return False

    time.sleep(1)

    # 실제 체결가 반영
    try:
        actual_entry = float(entry_order.get("average", signal.entry_price) or signal.entry_price)
    except Exception:
        actual_entry = signal.entry_price

    if actual_entry <= 0:
        actual_entry = signal.entry_price

    # 체결가 기준 SL/TP 재계산
    from config import STOP_LOSS_PCT
    if signal.side == "long":
        actual_sl = round(actual_entry * (1 - STOP_LOSS_PCT), 8)
        tp_pct    = (signal.take_profit - signal.entry_price) / signal.entry_price
        actual_tp = round(actual_entry * (1 + tp_pct), 8)
    else:
        actual_sl = round(actual_entry * (1 + STOP_LOSS_PCT), 8)
        tp_pct    = (signal.entry_price - signal.take_profit) / signal.entry_price
        actual_tp = round(actual_entry * (1 - tp_pct), 8)

    # 손절 주문
    sl_order = _place_order(
        exchange, symbol, signal.side, "stop_market",
        quantity, actual_sl, {"reduceOnly": True}
    )

    # 목표가 주문
    tp_order = _place_order(
        exchange, symbol, signal.side, "take_profit_market",
        quantity, actual_tp, {"reduceOnly": True}
    )

    if sl_order and tp_order:
        logger.info(
            f"[{symbol}] 전체 주문 완료 | "
            f"체결:{actual_entry:.4f} SL:{actual_sl:.4f} TP:{actual_tp:.4f}"
        )
        # ─── 매매일지 진입 기록 ───────────────────────────────────
        trade_id = record_entry(signal, quantity)
        return True

    # SL/TP 실패 → 긴급 청산
    logger.error(f"[{symbol}] SL/TP 주문 실패 → 긴급 청산")
    close_side_str = "sell" if signal.side == "long" else "buy"
    try:
        exchange.create_order(symbol, "market", close_side_str, quantity, None, {"reduceOnly": True})
        logger.info(f"[{symbol}] 긴급 청산 완료")
    except Exception as e:
        logger.critical(f"[{symbol}] 긴급 청산 실패! 수동 확인 필요: {e}")
    return False


def close_position(
    exchange, symbol: str, quantity: float, side: str,
    exit_price: float = 0.0, trade_id: str = "", exit_reason: str = "MANUAL"
) -> bool:
    """
    포지션 수동 청산 + 매매일지 청산 기록

    Args:
        trade_id   : journal.record_entry() 반환값
        exit_reason: "TP" | "SL" | "MANUAL"
    """
    close_side = "sell" if side == "long" else "buy"
    try:
        order = exchange.create_order(symbol, "market", close_side, quantity, None, {"reduceOnly": True})
        logger.info(f"[{symbol}] 포지션 청산 완료")

        # 청산가 확정
        actual_exit = float(order.get("average", exit_price) or exit_price)
        if trade_id and actual_exit > 0:
            record_exit(trade_id, actual_exit, exit_reason)

        return True
    except Exception as e:
        logger.error(f"[{symbol}] 청산 실패: {e}")
        return False
