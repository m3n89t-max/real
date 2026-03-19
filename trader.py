"""
바이낸스 선물 실거래 주문 실행 모듈 v3.1

v3.1 개선:
- TP1 체결 후 SL 수량 자동 조정 (기존: 전체수량 SL 유지 → 잔여수량 SL 재배치)
- TP2 거래소 주문 배치 (봇 종료 시에도 안전장치)
- 시드 100% 활용 (수익 전액 재투입)

주문 순서:
1. 총 노출도 확인
2. 시장가 진입
3. Stop Market (전체 수량 손절)
4. Take Profit Market TP1 (50% 수량, 1차 목표가)
5. Take Profit Market TP2 (50% 수량, 2차 목표가) ← 신규
6. 트레일링 스탑은 모니터링 루프에서 TP2 대체 관리
"""

import time
import logging
from signal_generator import TradeSignal
from risk_manager import (
    calculate_position_size, round_quantity,
    is_risk_acceptable, get_market_precision,
    check_total_exposure,
)
from journal import record_entry, record_exit
from rule_loader import R

logger = logging.getLogger(__name__)


def _place_order(exchange, symbol, side, order_type, quantity, price=None, params=None):
    """단일 주문 실행 래퍼"""
    params = params or {}
    try:
        order_side = "buy" if side == "long" else "sell"
        close_side = "sell" if side == "long" else "buy"

        if order_type == "market":
            order = exchange.create_order(symbol, "market", order_side, quantity, None, params)

        elif order_type == "limit":
            order = exchange.create_order(symbol, "limit", order_side, quantity, price, params)

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
    cooldown_tracker: dict = None,
) -> dict:
    """
    신호를 받아 실제 주문 실행 (v3.1)

    Returns:
        dict: 성공 시 진입 상세정보, 실패 시 빈 dict
    """
    symbol   = signal.symbol
    open_pos = open_positions or []
    open_count = len(open_pos)

    # ─── 0. 쿨다운 확인 ───────────────────────────────────────
    if cooldown_tracker and symbol in cooldown_tracker:
        cooldown_until = cooldown_tracker[symbol]
        if time.time() < cooldown_until:
            remaining = int(cooldown_until - time.time())
            logger.info(f"[{symbol}] 쿨다운 중 (잔여 {remaining}초) → 스킵")
            return {}

    # ─── 1. 중복 포지션 확인 ─────────────────────────────────────
    existing = [p["symbol"].split(":")[0] for p in open_pos]
    if symbol in existing:
        logger.info(f"[{symbol}] 이미 포지션 존재 → 스킵")
        return {}

    if open_count >= R.max_open_positions:
        logger.warning(f"최대 포지션 수 ({open_count}/{R.max_open_positions}) → 스킵")
        return {}

    # ─── 2. 잔고 확인 ────────────────────────────────────────────
    if balance <= 0:
        logger.error(f"잔고 없음: {balance:.2f} USDT")
        return {}

    # ─── 3. 수량 계산 ─────────────────────────────────────────
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
        return {}

    quantity = round_quantity(quantity, precision.get("amount", 0.001))
    if quantity < precision.get("min_qty", 0.001):
        logger.warning(f"[{symbol}] 최소 수량 미달: {quantity}")
        return {}

    # ─── 4. 총 노출도 확인 ────────────────────────────────────
    new_notional = signal.entry_price * quantity
    if not check_total_exposure(balance, open_pos, new_notional, signal.leverage, symbol):
        return {}

    # ─── 5. 리스크 최종 검증 ─────────────────────────────────────
    if not is_risk_acceptable(
        balance, signal.entry_price, signal.stop_loss,
        signal.take_profit, quantity, signal.leverage, symbol
    ):
        logger.warning(f"[{symbol}] 리스크 검증 실패 → 스킵")
        return {}

    # ─── 6. 분할 익절 수량 계산 ──────────────────────────────────
    min_qty = precision.get("min_qty", 0.001)
    if R.partial_tp_enabled:
        tp1_qty = round_quantity(quantity * R.tp1_ratio, precision.get("amount", 0.001))
        tp2_qty = round_quantity(quantity - tp1_qty, precision.get("amount", 0.001))
        if tp1_qty < min_qty:
            tp1_qty = quantity
            tp2_qty = 0
        if tp2_qty < min_qty:
            tp1_qty = quantity
            tp2_qty = 0
    else:
        tp1_qty = quantity
        tp2_qty = 0

    # ─── 7. 지정가 계산 ──────────────────────────────────────────
    sl_pct_display = abs(signal.entry_price - signal.stop_loss) / signal.entry_price * 100
    tp_pct_display = abs(signal.entry_price - signal.take_profit) / signal.entry_price * 100

    if R.limit_entry_enabled:
        # 현재가 대비 유리한 방향으로 limit_offset_pct 적용
        if signal.side == "long":
            limit_price = round(signal.entry_price * (1 - R.limit_offset_pct), 8)
        else:
            limit_price = round(signal.entry_price * (1 + R.limit_offset_pct), 8)
        order_type_str = "limit"
    else:
        limit_price    = None
        order_type_str = "market"

    logger.info(
        f"\n{'='*55}\n"
        f"[{symbol}] 주문 실행 (지정가모드={R.limit_entry_enabled})\n"
        f"  방향    : {signal.side.upper()}\n"
        f"  레버리지: {signal.leverage}x\n"
        f"  진입가  : {limit_price or signal.entry_price:.4f} USDT\n"
        f"  손절가  : {signal.stop_loss:.4f} USDT (-{sl_pct_display:.2f}%)\n"
        f"  목표가  : {signal.take_profit:.4f} USDT (+{tp_pct_display:.2f}%)\n"
        f"  수량    : {quantity} (TP1:{tp1_qty} / TP2:{tp2_qty})\n"
        f"  잔고    : {balance:.2f} USDT | ATR%:{signal.atr_pct:.3%}\n"
        f"{'='*55}"
    )

    entry_order = _place_order(exchange, symbol, signal.side, order_type_str, quantity, limit_price)
    if not entry_order:
        return {}

    order_id = entry_order.get("id", "")

    # ─── 지정가: 미체결 상태로 반환 (pending) ────────────────────
    if R.limit_entry_enabled:
        status = entry_order.get("status", "open")
        if status not in ("closed", "filled"):
            logger.info(
                f"[{symbol}] 지정가 주문 대기 중 | "
                f"order_id={order_id} limit={limit_price:.4f} "
                f"timeout={R.limit_timeout_sec}s"
            )
            return {
                "pending"    : True,
                "order_id"   : order_id,
                "limit_price": limit_price,
                "quantity"   : quantity,
                "tp1_qty"    : tp1_qty,
                "tp2_qty"    : tp2_qty,
                "placed_at"  : time.time(),
            }
        # 즉시 체결된 경우
        actual_entry = float(entry_order.get("average", limit_price) or limit_price)
    else:
        time.sleep(1)
        actual_entry = float(entry_order.get("average", signal.entry_price) or signal.entry_price)

    if actual_entry <= 0:
        actual_entry = signal.entry_price

    return _place_sl_tp_and_record(
        exchange, symbol, signal, actual_entry,
        quantity, tp1_qty, tp2_qty, cooldown_tracker
    )


def _place_sl_tp_and_record(
    exchange, symbol: str, signal,
    actual_entry: float, quantity: float,
    tp1_qty: float, tp2_qty: float,
    cooldown_tracker,
) -> dict:
    """체결 후 SL/TP 주문 배치 및 저널 기록 (지정가/시장가 공통)"""
    sl_pct = abs(signal.entry_price - signal.stop_loss) / signal.entry_price

    if signal.side == "long":
        actual_sl  = round(actual_entry * (1 - sl_pct), 8)
        risk_dist  = actual_entry - actual_sl
        actual_tp1 = round(actual_entry + risk_dist * R.tp1_rr, 8)
        actual_tp2 = round(actual_entry + risk_dist * R.tp2_rr, 8)
    else:
        actual_sl  = round(actual_entry * (1 + sl_pct), 8)
        risk_dist  = actual_sl - actual_entry
        actual_tp1 = round(actual_entry - risk_dist * R.tp1_rr, 8)
        actual_tp2 = round(actual_entry - risk_dist * R.tp2_rr, 8)

    sl_order = _place_order(
        exchange, symbol, signal.side, "stop_market",
        quantity, actual_sl, {"reduceOnly": True}
    )
    tp1_order = _place_order(
        exchange, symbol, signal.side, "take_profit_market",
        tp1_qty, actual_tp1, {"reduceOnly": True}
    )
    tp2_order = {}
    if tp2_qty > 0:
        tp2_order = _place_order(
            exchange, symbol, signal.side, "take_profit_market",
            tp2_qty, actual_tp2, {"reduceOnly": True}
        )

    if sl_order and tp1_order:
        tp2_info = f" | TP2:{actual_tp2:.4f}({tp2_qty})" if tp2_qty > 0 else ""
        logger.info(
            f"[{symbol}] 전체 주문 완료 | "
            f"체결:{actual_entry:.4f} SL:{actual_sl:.4f} "
            f"TP1:{actual_tp1:.4f}({tp1_qty}){tp2_info}"
        )
        trade_id = record_entry(signal, quantity)
        if cooldown_tracker is not None:
            cooldown_tracker[symbol] = time.time() + R.entry_cooldown_sec
        return {
            "trade_id"    : trade_id,
            "actual_entry": actual_entry,
            "actual_sl"   : actual_sl,
            "actual_tp1"  : actual_tp1,
            "actual_tp2"  : actual_tp2,
            "quantity"    : quantity,
            "tp1_qty"     : tp1_qty,
            "tp2_qty"     : tp2_qty,
        }

    # SL/TP 실패 → 긴급 청산
    logger.error(f"[{symbol}] SL/TP 주문 실패 → 긴급 청산")
    close_side_str = "sell" if signal.side == "long" else "buy"
    try:
        exchange.create_order(symbol, "market", close_side_str, quantity, None, {"reduceOnly": True})
        logger.info(f"[{symbol}] 긴급 청산 완료")
    except Exception as e:
        logger.critical(f"[{symbol}] 긴급 청산 실패! 수동 확인 필요: {e}")
    return {}


def check_pending_limit(exchange, symbol: str, pending: dict, signal,
                        cooldown_tracker) -> dict:
    """
    지정가 주문 체결 여부 확인.

    Returns:
        - 체결됨  : {trade_id, actual_entry, actual_sl, ...}
        - 미체결  : {"pending": True}
        - 타임아웃: {"cancelled": True}
    """
    order_id  = pending["order_id"]
    placed_at = pending["placed_at"]
    quantity  = pending["quantity"]
    tp1_qty   = pending["tp1_qty"]
    tp2_qty   = pending["tp2_qty"]

    # 타임아웃 → 주문 취소
    if time.time() - placed_at > R.limit_timeout_sec:
        try:
            exchange.cancel_order(order_id, symbol)
            logger.info(f"[{symbol}] 지정가 주문 취소 (타임아웃 {R.limit_timeout_sec}s)")
        except Exception as e:
            logger.warning(f"[{symbol}] 주문 취소 실패(이미 처리됨?): {e}")
        return {"cancelled": True}

    # 체결 상태 조회
    try:
        order  = exchange.fetch_order(order_id, symbol)
        status = order.get("status", "open")
        filled = float(order.get("filled", 0) or 0)

        if status in ("closed", "filled") or filled >= quantity * 0.99:
            actual_entry = float(order.get("average", pending["limit_price"]) or pending["limit_price"])
            logger.info(f"[{symbol}] 지정가 체결 확인 @ {actual_entry:.4f}")
            return _place_sl_tp_and_record(
                exchange, symbol, signal, actual_entry,
                quantity, tp1_qty, tp2_qty, cooldown_tracker
            )

        remaining = int(R.limit_timeout_sec - (time.time() - placed_at))
        logger.info(f"[{symbol}] 지정가 대기 중 (잔여 {remaining}s | filled={filled}/{quantity})")
        return {"pending": True}

    except Exception as e:
        logger.error(f"[{symbol}] 주문 조회 실패: {e}")
        return {"pending": True}


def close_position(
    exchange, symbol: str, quantity: float, side: str,
    exit_price: float = 0.0, trade_id: str = "", exit_reason: str = "MANUAL"
) -> bool:
    """포지션 수동 청산 + 매매일지 청산 기록"""
    close_side = "sell" if side == "long" else "buy"
    try:
        order = exchange.create_order(symbol, "market", close_side, quantity, None, {"reduceOnly": True})
        logger.info(f"[{symbol}] 포지션 청산 완료")

        actual_exit = float(order.get("average", exit_price) or exit_price)
        if trade_id and actual_exit > 0:
            record_exit(trade_id, actual_exit, exit_reason)

        return True
    except Exception as e:
        logger.error(f"[{symbol}] 청산 실패: {e}")
        return False


# ══════════════════════════════════════════════════════════════
# TP1 체결 감지 → SL 수량 재조정
# ══════════════════════════════════════════════════════════════

def adjust_sl_after_tp1(exchange, symbol: str, side: str,
                        remaining_qty: float, current_sl: float) -> bool:
    """
    TP1(50%) 체결 후:
    1. 기존 SL(전체수량) 취소
    2. 잔여수량으로 새 SL 주문 배치
    """
    try:
        open_orders = exchange.fetch_open_orders(symbol)
        cancelled = 0
        for order in open_orders:
            if order.get("type") == "stop_market":
                exchange.cancel_order(order["id"], symbol)
                cancelled += 1
                logger.info(f"[{symbol}] 기존 SL 취소 (TP1 체결 후 수량조정): {order['id']}")

        if remaining_qty > 0:
            close_side = "sell" if side == "long" else "buy"
            exchange.create_order(
                symbol, "stop_market", close_side, remaining_qty, None,
                {"stopPrice": current_sl, "reduceOnly": True}
            )
            logger.info(
                f"[{symbol}] SL 재배치 완료 | "
                f"잔여수량:{remaining_qty} SL:{current_sl:.4f}"
            )
        return True
    except Exception as e:
        logger.error(f"[{symbol}] TP1 후 SL 조정 실패: {e}")
        return False


# ══════════════════════════════════════════════════════════════
# 트레일링 스탑 / 본전 이동 로직
# ══════════════════════════════════════════════════════════════

def check_trailing_stop(
    exchange, symbol: str, side: str,
    entry_price: float, current_price: float,
    stop_loss: float, atr_pct: float,
    quantity: float,
) -> dict:
    """
    포지션 모니터링 시 호출 — 트레일링/본전이동 판단

    Returns:
        {"action": "none"/"move_sl", "new_sl": float, "reason": str}
    """
    if not R.trailing_enabled:
        return {"action": "none", "new_sl": stop_loss, "reason": ""}

    risk_distance = abs(entry_price - stop_loss)
    if risk_distance <= 0:
        return {"action": "none", "new_sl": stop_loss, "reason": ""}

    if side == "long":
        current_pnl = current_price - entry_price
    else:
        current_pnl = entry_price - current_price

    current_rr = current_pnl / risk_distance if risk_distance > 0 else 0

    # 본전 이동: RR >= breakeven_rr
    if current_rr >= R.breakeven_rr and side == "long" and stop_loss < entry_price:
        return {
            "action": "move_sl",
            "new_sl": entry_price,
            "reason": f"본전이동 (RR={current_rr:.1f})"
        }
    if current_rr >= R.breakeven_rr and side == "short" and stop_loss > entry_price:
        return {
            "action": "move_sl",
            "new_sl": entry_price,
            "reason": f"본전이동 (RR={current_rr:.1f})"
        }

    # 트레일링: RR >= activation_rr
    if current_rr >= R.trailing_activation_rr:
        trail_dist = entry_price * atr_pct * R.trailing_distance_atr
        if trail_dist <= 0:
            trail_dist = risk_distance

        if side == "long":
            new_trail_sl = current_price - trail_dist
            if new_trail_sl > stop_loss:
                return {
                    "action": "move_sl",
                    "new_sl": round(new_trail_sl, 8),
                    "reason": f"트레일링 (RR={current_rr:.1f}, SL→{new_trail_sl:.4f})"
                }
        else:
            new_trail_sl = current_price + trail_dist
            if new_trail_sl < stop_loss:
                return {
                    "action": "move_sl",
                    "new_sl": round(new_trail_sl, 8),
                    "reason": f"트레일링 (RR={current_rr:.1f}, SL→{new_trail_sl:.4f})"
                }

    return {"action": "none", "new_sl": stop_loss, "reason": ""}


def update_stop_loss(exchange, symbol: str, side: str, quantity: float,
                     old_sl: float, new_sl: float) -> bool:
    """기존 SL 주문 취소 후 새 SL 주문 배치"""
    try:
        open_orders = exchange.fetch_open_orders(symbol)
        for order in open_orders:
            if order.get("type") == "stop_market":
                exchange.cancel_order(order["id"], symbol)
                logger.info(f"[{symbol}] 기존 SL 주문 취소: {order['id']}")
                break

        close_side = "sell" if side == "long" else "buy"
        exchange.create_order(
            symbol, "stop_market", close_side, quantity, None,
            {"stopPrice": new_sl, "reduceOnly": True}
        )
        logger.info(f"[{symbol}] SL 이동 완료: {old_sl:.4f} → {new_sl:.4f}")
        return True
    except Exception as e:
        logger.error(f"[{symbol}] SL 이동 실패: {e}")
        return False
