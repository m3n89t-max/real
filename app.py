"""
app.py — Railway 배포용 통합 엔트리포인트

- 트레이딩 봇: 백그라운드 스레드
- 대시보드:   Flask 웹서버 (포트 $PORT)
"""

import os
import sys
import json
import threading
import logging
import time
from datetime import datetime
from flask import Flask, render_template, jsonify, request

# ── 로깅 설정 ──────────────────────────────────────────────────
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trading_bot.log")

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

setup_logging()

from journal import (
    load_all_records, get_open_trades, get_closed_trades,
    _calculate_stats, DETAIL_FILE, STATS_FILE
)
from rule_loader import R, RULES_FILE, GUIDE_FILE

# ── 봇 상태 공유 객체 ──────────────────────────────────────────
bot_state = {
    "running": False,
    "started_at": None,
    "scan_count": 0,
    "last_scan": None,
    "balance": 0.0,
    "error": None,
}
bot_state_lock = threading.Lock()

# ── Flask 앱 ───────────────────────────────────────────────────
app = Flask(__name__)


# ══════════════════════════════════════════════════════════════
# 대시보드 API
# ══════════════════════════════════════════════════════════════

def _load_details() -> dict:
    if os.path.exists(DETAIL_FILE):
        try:
            with open(DETAIL_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _load_stats() -> dict:
    if os.path.exists(STATS_FILE):
        try:
            with open(STATS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    closed = get_closed_trades()
    return _calculate_stats(closed)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    with bot_state_lock:
        return jsonify({**bot_state})


@app.route("/api/stats")
def api_stats():
    return jsonify(_load_stats())


@app.route("/api/trades")
def api_trades():
    details = _load_details()
    records = load_all_records()
    result  = []
    for r in reversed(records):
        d = details.get(r["trade_id"], {})
        result.append({**r, **d})
    return jsonify(result)


@app.route("/api/open")
def api_open():
    details  = _load_details()
    open_pos = get_open_trades()
    result   = []
    for r in open_pos:
        d = details.get(r["trade_id"], {})
        result.append({**r, **d})
    return jsonify(result)


@app.route("/api/pnl_chart")
def api_pnl_chart():
    """최근 50거래 PnL 차트 데이터"""
    closed = get_closed_trades()
    recent = list(reversed(closed))[:50]
    labels = [r.get("exit_time", "")[:10] for r in recent]
    pnls   = [float(r.get("pnl_usdt", 0)) for r in recent]
    cumulative = []
    total = 0.0
    for p in pnls:
        total += p
        cumulative.append(round(total, 4))
    return jsonify({"labels": labels, "pnls": pnls, "cumulative": cumulative})


@app.route("/api/logs")
def api_logs():
    n      = int(request.args.get("n", 150))
    offset = int(request.args.get("offset", 0))
    try:
        if not os.path.exists(LOG_FILE):
            return jsonify({"lines": [], "total": 0})
        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
        total = len(all_lines)
        if offset > 0:
            new_lines = [l.rstrip("\n") for l in all_lines[offset:]]
        else:
            new_lines = [l.rstrip("\n") for l in all_lines[max(0, total - n):]]
        return jsonify({"lines": new_lines, "total": total})
    except Exception as e:
        return jsonify({"lines": [f"로그 오류: {e}"], "total": 0})


@app.route("/api/rules")
def api_rules():
    return jsonify(R.get_raw())


@app.route("/api/rules/save", methods=["POST"])
def api_rules_save():
    try:
        data = request.get_json(force=True)
        R.save_rules(data)
        return jsonify({"ok": True, "msg": "저장 완료 — 봇에 즉시 반영됩니다."})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/bot/stop", methods=["POST"])
def api_bot_stop():
    with bot_state_lock:
        bot_state["running"] = False
    return jsonify({"ok": True, "msg": "봇 중지 요청됨"})


@app.route("/api/bot/start", methods=["POST"])
def api_bot_start():
    with bot_state_lock:
        if bot_state["running"]:
            return jsonify({"ok": False, "msg": "이미 실행 중입니다."})
    t = threading.Thread(target=_run_bot, daemon=True)
    t.start()
    return jsonify({"ok": True, "msg": "봇 시작됨"})


# ══════════════════════════════════════════════════════════════
# 봇 실행 (백그라운드 스레드)
# ══════════════════════════════════════════════════════════════

def _run_bot():
    """트레이딩 봇 메인 루프 — 별도 스레드에서 실행"""
    logger = logging.getLogger("bot")

    # 지연 임포트 (순환 방지)
    from collections import defaultdict
    from config import SYMBOLS
    from data_fetcher import (
        initialize_exchange, fetch_all_timeframes,
        get_account_balance, get_open_positions, set_leverage_dynamic,
        get_current_price,
    )
    from signal_generator import generate_signal
    from trader import execute_trade, check_trailing_stop, update_stop_loss, adjust_sl_after_tp1, check_pending_limit
    from journal import print_stats, get_open_trades, update_stats

    with bot_state_lock:
        bot_state["running"]    = True
        bot_state["started_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        bot_state["error"]      = None

    logger.info("봇 스레드 시작")

    # ── 쿨다운 / 포지션 트래커 (main.py와 동일 구조) ──────────
    class CooldownManager:
        def __init__(self):
            self.cooldowns = {}
            self.consecutive_losses = defaultdict(int)
            self._lock = threading.Lock()

        def is_cooled_down(self, symbol):
            with self._lock:
                if symbol in self.cooldowns:
                    if time.time() < self.cooldowns[symbol]:
                        return False
                    del self.cooldowns[symbol]
            return True

        def set_cooldown(self, symbol, seconds):
            with self._lock:
                self.cooldowns[symbol] = time.time() + seconds

        def record_loss(self, symbol):
            with self._lock:
                self.consecutive_losses[symbol] += 1
                if self.consecutive_losses[symbol] >= R.max_consecutive_losses:
                    self.cooldowns[symbol] = time.time() + 3600

        def record_win(self, symbol):
            with self._lock:
                self.consecutive_losses[symbol] = 0

        def get_remaining(self, symbol):
            with self._lock:
                if symbol in self.cooldowns:
                    return max(0, int(self.cooldowns[symbol] - time.time()))
            return 0

    class PositionTracker:
        def __init__(self):
            self.positions = {}
            self._lock = threading.Lock()

        def add(self, symbol, side, entry_price, stop_loss,
                quantity, tp1_qty, tp2_qty, atr_pct, trade_id):
            with self._lock:
                self.positions[symbol] = {
                    "side": side, "entry_price": entry_price,
                    "original_sl": stop_loss, "current_sl": stop_loss,
                    "quantity": quantity, "tp1_qty": tp1_qty, "tp2_qty": tp2_qty,
                    "remaining_qty": quantity, "tp1_filled": False,
                    "atr_pct": atr_pct, "trade_id": trade_id,
                }

        def remove(self, symbol):
            with self._lock:
                self.positions.pop(symbol, None)

        def get_all(self):
            with self._lock:
                return {k: dict(v) for k, v in self.positions.items()}

        def update_sl(self, symbol, new_sl):
            with self._lock:
                if symbol in self.positions:
                    self.positions[symbol]["current_sl"] = new_sl

        def mark_tp1_filled(self, symbol):
            with self._lock:
                if symbol in self.positions:
                    p = self.positions[symbol]
                    p["tp1_filled"] = True
                    p["remaining_qty"] = p["tp2_qty"]

        def update_remaining_qty(self, symbol, qty):
            with self._lock:
                if symbol in self.positions:
                    self.positions[symbol]["remaining_qty"] = qty

    class PendingOrderTracker:
        """지정가 미체결 주문 추적"""
        def __init__(self):
            self.orders = {}   # symbol → {order_id, limit_price, quantity, tp1_qty, tp2_qty, placed_at, signal}
            self._lock  = threading.Lock()

        def add(self, symbol, pending: dict, signal):
            with self._lock:
                self.orders[symbol] = {**pending, "signal": signal}

        def remove(self, symbol):
            with self._lock:
                self.orders.pop(symbol, None)

        def get_all(self):
            with self._lock:
                return dict(self.orders)

        def has(self, symbol):
            with self._lock:
                return symbol in self.orders

    cooldown_mgr  = CooldownManager()
    pos_tracker   = PositionTracker()
    pending_tracker = PendingOrderTracker()

    def _get_pos_qty(exch, sym):
        try:
            for p in exch.fetch_positions([sym]):
                if float(p.get("contracts", 0)) != 0:
                    return abs(float(p["contracts"]))
        except Exception:
            pass
        return 0.0

    def monitor_positions(exch):
        for symbol, info in pos_tracker.get_all().items():
            try:
                cur = get_current_price(exch, symbol)
                if cur <= 0:
                    continue
                if not info["tp1_filled"] and info["tp2_qty"] > 0:
                    actual = _get_pos_qty(exch, symbol)
                    if 0 < actual <= info["tp2_qty"] * 1.05:
                        pos_tracker.mark_tp1_filled(symbol)
                        adjust_sl_after_tp1(exch, symbol, info["side"], actual, info["current_sl"])
                        pos_tracker.update_remaining_qty(symbol, actual)
                        cooldown_mgr.record_win(symbol)

                remaining = info["remaining_qty"]
                if remaining <= 0:
                    continue
                res = check_trailing_stop(
                    exch, symbol, info["side"],
                    info["entry_price"], cur,
                    info["current_sl"], info["atr_pct"], remaining,
                )
                if res["action"] == "move_sl":
                    if update_stop_loss(exch, symbol, info["side"],
                                        remaining, info["current_sl"], res["new_sl"]):
                        pos_tracker.update_sl(symbol, res["new_sl"])
            except Exception as e:
                logger.error(f"[{symbol}] 모니터링 오류: {e}")

    def _get_last_fill_price(exch, symbol):
        """해당 심볼의 가장 최근 체결가 조회"""
        try:
            trades = exch.fetch_my_trades(symbol, limit=10)
            if trades:
                trades.sort(key=lambda t: t["timestamp"], reverse=True)
                return float(trades[0]["price"])
        except Exception as e:
            logger.warning(f"[{symbol}] 체결가 조회 실패: {e}")
        try:
            return get_current_price(exch, symbol)
        except Exception:
            return None

    def sync_positions(exch):
        try:
            open_syms = {p["symbol"].split(":")[0] for p in get_open_positions(exch)}
            for sym in list(pos_tracker.get_all().keys()):
                if sym not in open_syms:
                    info     = pos_tracker.get_all().get(sym, {})
                    trade_id = info.get("trade_id")
                    side     = info.get("side", "long")

                    # 실제 청산가 조회
                    exit_price = _get_last_fill_price(exch, sym)

                    # 청산 사유 판단
                    cur_sl  = info.get("current_sl", 0)
                    orig_sl = info.get("original_sl", 0)
                    if cur_sl > orig_sl:
                        # 트레일링 SL이 올라갔다 = TP 도달 후 청산
                        exit_reason = "TP"
                        cooldown_mgr.record_win(sym)
                    elif exit_price:
                        ep = info.get("entry_price", exit_price)
                        if side == "long":
                            exit_reason = "TP" if exit_price > ep else "SL"
                        else:
                            exit_reason = "TP" if exit_price < ep else "SL"
                        if exit_reason == "TP":
                            cooldown_mgr.record_win(sym)
                        else:
                            cooldown_mgr.record_loss(sym)
                            cooldown_mgr.set_cooldown(sym, R.loss_cooldown_sec)
                    else:
                        exit_reason = "SL"
                        cooldown_mgr.record_loss(sym)
                        cooldown_mgr.set_cooldown(sym, R.loss_cooldown_sec)

                    # 저널 청산 기록
                    if trade_id and exit_price:
                        try:
                            from journal import record_exit
                            record_exit(trade_id, exit_price, exit_reason)
                            logger.info(f"[{sym}] 저널 동기화: {exit_reason} @ {exit_price:.4f}")
                        except Exception as je:
                            logger.error(f"[{sym}] 저널 청산 기록 실패: {je}")

                    pos_tracker.remove(sym)
        except Exception as e:
            logger.error(f"포지션 동기화 오류: {e}")

    def reconcile_journal(exch):
        """봇 시작 시 저널 OPEN 거래를 거래소 실제 포지션과 대조해 미동기화 항목 일괄 청산"""
        try:
            from journal import get_open_trades, record_exit
            open_journal = get_open_trades()
            if not open_journal:
                return
            real_positions = get_open_positions(exch)
            real_syms = {p["symbol"].split(":")[0] for p in real_positions}
            stale = [t for t in open_journal if t["symbol"] not in real_syms]
            if not stale:
                return
            logger.info(f"미동기화 OPEN 거래 {len(stale)}건 발견 → 거래소 체결가로 정산")
            for t in stale:
                sym   = t["symbol"]
                tid   = t["trade_id"]
                side  = t["side"]
                ep    = float(t["entry_price"])
                exit_price = _get_last_fill_price(exch, sym) or ep
                exit_reason = "SL" if (
                    (side == "long"  and exit_price < ep) or
                    (side == "short" and exit_price > ep)
                ) else "TP"
                record_exit(tid, exit_price, exit_reason)
                logger.info(f"[{sym}] 저널 복구: {tid} {exit_reason} @ {exit_price:.4f}")
        except Exception as e:
            logger.error(f"저널 복구 오류: {e}")

    # ── 초기화 ────────────────────────────────────────────────
    try:
        exchange = initialize_exchange()
        logger.info("거래소 연결 완료")
    except Exception as e:
        with bot_state_lock:
            bot_state["running"] = False
            bot_state["error"]   = str(e)
        logger.critical(f"거래소 초기화 실패: {e}")
        return

    update_stats()
    reconcile_journal(exchange)   # 미동기화 OPEN 거래 즉시 정산

    scan_count = 0

    while True:
        with bot_state_lock:
            if not bot_state["running"]:
                logger.info("봇 중지 요청 감지 → 종료")
                break

        scan_count += 1
        with bot_state_lock:
            bot_state["scan_count"] = scan_count
            bot_state["last_scan"]  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        try:
            sync_positions(exchange)
            monitor_positions(exchange)

            # ── 지정가 미체결 주문 체결 확인 ────────────────────
            for sym, pend in list(pending_tracker.get_all().items()):
                sig = pend.pop("signal")
                result = check_pending_limit(
                    exchange, sym, pend, sig, cooldown_mgr.cooldowns
                )
                if result.get("cancelled"):
                    pending_tracker.remove(sym)
                    logger.info(f"[{sym}] 지정가 주문 취소됨")
                elif not result.get("pending"):
                    # 체결 완료 → pos_tracker에 등록
                    pending_tracker.remove(sym)
                    pos_tracker.add(
                        sym, sig.side,
                        result["actual_entry"], result["actual_sl"],
                        result["quantity"], result["tp1_qty"], result["tp2_qty"],
                        sig.atr_pct, result["trade_id"],
                    )
                else:
                    # 아직 미체결 → signal 다시 넣기
                    pend["signal"] = sig

            signals = []
            for symbol in SYMBOLS:
                if not cooldown_mgr.is_cooled_down(symbol):
                    continue
                if pending_tracker.has(symbol):   # 지정가 대기 중 → 스킵
                    continue
                try:
                    all_tf = fetch_all_timeframes(exchange, symbol)
                    if not all_tf:
                        continue
                    sig = generate_signal(symbol, all_tf)
                    if sig:
                        signals.append(sig)
                    time.sleep(0.3)
                except Exception as e:
                    logger.error(f"[{symbol}] 스캔 오류: {e}")

            signals.sort(key=lambda s: (s.leverage, s.confidence), reverse=True)
            balance  = get_account_balance(exchange)
            open_pos = get_open_positions(exchange)

            with bot_state_lock:
                bot_state["balance"] = round(balance, 2)

            for sig in signals:
                set_leverage_dynamic(exchange, sig.symbol, sig.leverage)
                res = execute_trade(
                    exchange, sig,
                    balance=balance,
                    open_positions=open_pos,
                    cooldown_tracker=cooldown_mgr.cooldowns,
                )
                if res.get("pending"):
                    # 지정가 주문 대기 중 → pending_tracker에 등록
                    pending_tracker.add(sig.symbol, res, sig)
                    cooldown_mgr.set_cooldown(sig.symbol, R.entry_cooldown_sec)
                elif res:
                    # 즉시 체결 (시장가 or 지정가 즉시 체결)
                    pos_tracker.add(
                        sig.symbol, sig.side,
                        res["actual_entry"], res["actual_sl"],
                        res["quantity"], res["tp1_qty"], res["tp2_qty"],
                        sig.atr_pct, res["trade_id"],
                    )
                    cooldown_mgr.set_cooldown(sig.symbol, R.entry_cooldown_sec)
                    balance  = get_account_balance(exchange)
                    open_pos = get_open_positions(exchange)
                time.sleep(0.5)

        except Exception as e:
            logger.error(f"메인 루프 오류: {e}", exc_info=True)
            with bot_state_lock:
                bot_state["error"] = str(e)

        if R.check_and_reload():
            logger.info("지침서 파라미터 재로드 완료")

        time.sleep(R.scan_interval_sec)

    with bot_state_lock:
        bot_state["running"] = False
    logger.info("봇 스레드 종료")


# ══════════════════════════════════════════════════════════════
# 메인
# ══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    # 봇 자동 시작 (API 키가 있을 때만)
    from config import BINANCE_API_KEY, BINANCE_SECRET_KEY
    if BINANCE_API_KEY and BINANCE_SECRET_KEY:
        t = threading.Thread(target=_run_bot, daemon=True)
        t.start()
        logging.getLogger("app").info("봇 백그라운드 스레드 시작됨")
    else:
        logging.getLogger("app").warning("API 키 없음 → 봇 미시작 (대시보드만 실행)")

    port = int(os.environ.get("PORT", 5000))
    logging.getLogger("app").info(f"대시보드 서버 시작: http://0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
