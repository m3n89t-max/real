"""
매매일지 모듈

기록 항목:
- 진입: 시간, 코인, 방향, 진입가, 수량, 레버리지, 파동정보, PRG/RSI 확인 여부
- 청산: 청산가, PnL(USDT/%), 청산 사유(TP/SL/수동)
- 통계: 승률, 평균 손익비, 누적 수익, 최대 손실

파일: trade_journal.csv (누적 저장)
"""

import csv
import json
import os
import logging
from datetime import datetime
from dataclasses import dataclass, asdict, field
from typing import Optional

logger = logging.getLogger(__name__)

JOURNAL_FILE  = "trade_journal.csv"
STATS_FILE    = "trade_stats.json"
DETAIL_FILE   = "trade_details.json"   # 진입 근거 전문 + 목표가 상세

JOURNAL_FIELDS = [
    "trade_id",        # 고유 번호
    "entry_time",      # 진입 시간
    "exit_time",       # 청산 시간
    "symbol",          # 코인
    "side",            # long / short
    "entry_price",     # 진입가
    "exit_price",      # 청산가
    "quantity",        # 수량
    "leverage",        # 레버리지
    "stop_loss",       # 손절가
    "take_profit",     # 목표가
    "pnl_usdt",        # 손익 (USDT)
    "pnl_pct",         # 손익 (%)
    "exit_reason",     # TP / SL / MANUAL
    "wave_type",       # impulse / zigzag / flat
    "wave_position",   # wave3 / wave5 / wave_c
    "trend_4h",        # 상위 추세
    "trend_1h",
    "trend_15m",
    "prg_confirmed",   # PRG 피보나치 중첩 여부
    "rsi_confirmed",   # RSI 다이버전스 여부
    "confidence",      # 신뢰도
    "status",          # OPEN / CLOSED
    "entry_reason",    # 진입 근거 요약 (첫 줄)
    "reason_short",    # 한 줄 매매 사유 (로그용)
    "tp_pct",          # 목표가 거리 %
    "sl_pct",          # 손절가 거리 %
    "rr_ratio",        # 손익비
]


@dataclass
class TradeRecord:
    trade_id: str       = ""
    entry_time: str     = ""
    exit_time: str      = ""
    symbol: str         = ""
    side: str           = ""
    entry_price: float  = 0.0
    exit_price: float   = 0.0
    quantity: float     = 0.0
    leverage: int       = 4
    stop_loss: float    = 0.0
    take_profit: float  = 0.0
    pnl_usdt: float     = 0.0
    pnl_pct: float      = 0.0
    exit_reason: str    = ""
    wave_type: str      = ""
    wave_position: str  = ""
    trend_4h: str       = ""
    trend_1h: str       = ""
    trend_15m: str      = ""
    prg_confirmed: bool = False
    rsi_confirmed: bool = False
    confidence: float   = 0.0
    status: str         = "OPEN"
    entry_reason: str   = ""
    reason_short: str   = ""
    tp_pct: float       = 0.0
    sl_pct: float       = 0.0
    rr_ratio: float     = 0.0


# ══════════════════════════════════════════════════════════════
# 파일 초기화
# ══════════════════════════════════════════════════════════════

def _ensure_journal_file():
    """CSV 파일이 없으면 헤더와 함께 생성"""
    if not os.path.exists(JOURNAL_FILE):
        with open(JOURNAL_FILE, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=JOURNAL_FIELDS)
            writer.writeheader()
        logger.info(f"매매일지 파일 생성: {JOURNAL_FILE}")


def _next_trade_id() -> str:
    """순차 trade_id 생성 (T001, T002 ...)"""
    records = load_all_records()
    n = len(records) + 1
    return f"T{n:04d}"


# ══════════════════════════════════════════════════════════════
# 기록 / 수정
# ══════════════════════════════════════════════════════════════

def record_entry(signal, quantity: float) -> str:
    """
    진입 시 일지 기록

    Args:
        signal  : TradeSignal 객체
        quantity: 실제 주문 수량

    Returns:
        trade_id (청산 시 참조용)
    """
    _ensure_journal_file()
    trade_id = _next_trade_id()

    ep   = signal.entry_price
    tp   = signal.take_profit
    sl   = signal.stop_loss
    tp_pct  = round(abs(ep - tp) / ep * 100, 2) if ep else 0
    sl_pct  = round(abs(ep - sl) / ep * 100, 2) if ep else 0
    rr      = round(abs(ep - tp) / max(abs(ep - sl), 0.0001), 2)

    reason_summary = signal.entry_reason.split("\n")[0] if signal.entry_reason else ""

    record = TradeRecord(
        trade_id      = trade_id,
        entry_time    = datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        symbol        = signal.symbol,
        side          = signal.side,
        entry_price   = signal.entry_price,
        quantity      = quantity,
        leverage      = signal.leverage,
        stop_loss     = signal.stop_loss,
        take_profit   = signal.take_profit,
        wave_type     = signal.wave_type,
        wave_position = signal.wave_position,
        trend_4h      = getattr(signal, "trend_4h", ""),
        trend_1h      = getattr(signal, "trend_1h", ""),
        trend_15m     = getattr(signal, "trend_15m", ""),
        prg_confirmed = signal.prg_confirmed,
        rsi_confirmed = signal.rsi_confirmed,
        confidence    = signal.confidence,
        status        = "OPEN",
        entry_reason  = reason_summary,
        reason_short  = getattr(signal, "reason", ""),
        tp_pct        = tp_pct,
        sl_pct        = sl_pct,
        rr_ratio      = rr,
    )

    _append_record(record)
    _save_detail(trade_id, signal, quantity)
    logger.info(f"[일지] 진입 기록 | {trade_id} | {signal.symbol} {signal.side.upper()} | {signal.entry_price:.4f}")
    return trade_id


def _save_detail(trade_id: str, signal, quantity: float):
    """진입 근거 전문 + 목표가 상세 JSON 저장"""
    try:
        details = {}
        if os.path.exists(DETAIL_FILE):
            with open(DETAIL_FILE, "r", encoding="utf-8") as f:
                details = json.load(f)

        details[trade_id] = {
            "trade_id"     : trade_id,
            "symbol"       : signal.symbol,
            "side"         : signal.side,
            "entry_price"  : signal.entry_price,
            "stop_loss"    : signal.stop_loss,
            "take_profit"  : signal.take_profit,
            "quantity"     : quantity,
            "leverage"     : signal.leverage,
            "confidence"   : round(signal.confidence, 4),
            "wave_type"    : signal.wave_type,
            "wave_position": signal.wave_position,
            "prg_confirmed": signal.prg_confirmed,
            "rsi_confirmed": signal.rsi_confirmed,
            "entry_reason" : signal.entry_reason,   # 전문 저장
            "sl_pct"       : round(abs(signal.entry_price - signal.stop_loss) / signal.entry_price * 100, 2),
            "tp_pct"       : round(abs(signal.entry_price - signal.take_profit) / signal.entry_price * 100, 2),
            "rr_ratio"     : round(
                abs(signal.entry_price - signal.take_profit) /
                max(abs(signal.entry_price - signal.stop_loss), 0.0001), 2
            ),
            "entry_time"   : datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

        with open(DETAIL_FILE, "w", encoding="utf-8") as f:
            json.dump(details, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"[일지] 상세 저장 실패: {e}")


def record_exit(trade_id: str, exit_price: float, exit_reason: str = "MANUAL"):
    """
    청산 시 일지 업데이트

    Args:
        trade_id   : 진입 시 발급된 ID
        exit_price : 실제 청산가
        exit_reason: "TP" | "SL" | "MANUAL"
    """
    records = load_all_records()
    updated = False

    for r in records:
        if r["trade_id"] == trade_id and r["status"] == "OPEN":
            ep    = float(r["entry_price"])
            qty   = float(r["quantity"])
            side  = r["side"]
            lev   = int(r["leverage"])

            if side == "long":
                pnl_usdt = (exit_price - ep) * qty
                pnl_pct  = (exit_price - ep) / ep * 100 * lev
            else:
                pnl_usdt = (ep - exit_price) * qty
                pnl_pct  = (ep - exit_price) / ep * 100 * lev

            r["exit_time"]   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            r["exit_price"]  = exit_price
            r["pnl_usdt"]    = round(pnl_usdt, 4)
            r["pnl_pct"]     = round(pnl_pct, 2)
            r["exit_reason"] = exit_reason
            r["status"]      = "CLOSED"
            updated = True

            result_str = "익절" if pnl_usdt >= 0 else "손절"
            logger.info(
                f"[일지] 청산 기록 | {trade_id} | {r['symbol']} | "
                f"{result_str} {pnl_usdt:+.4f} USDT ({pnl_pct:+.2f}%) | {exit_reason}"
            )
            break

    if updated:
        _overwrite_records(records)
        # detail 파일에 청산 정보 추가
        try:
            if os.path.exists(DETAIL_FILE):
                with open(DETAIL_FILE, "r", encoding="utf-8") as f:
                    details = json.load(f)
                if trade_id in details:
                    details[trade_id]["exit_price"]  = exit_price
                    details[trade_id]["exit_reason"] = exit_reason
                    details[trade_id]["exit_time"]   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    with open(DETAIL_FILE, "w", encoding="utf-8") as f:
                        json.dump(details, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"[일지] 상세 청산 업데이트 실패: {e}")
    else:
        logger.warning(f"[일지] trade_id {trade_id} OPEN 기록 없음")

    # 통계 갱신
    update_stats()


def _append_record(record: TradeRecord):
    """CSV에 한 줄 추가"""
    with open(JOURNAL_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=JOURNAL_FIELDS)
        row = asdict(record)
        writer.writerow(row)


def _overwrite_records(records: list[dict]):
    """전체 CSV 재작성"""
    with open(JOURNAL_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=JOURNAL_FIELDS)
        writer.writeheader()
        writer.writerows(records)


# ══════════════════════════════════════════════════════════════
# 조회
# ══════════════════════════════════════════════════════════════

def load_all_records() -> list[dict]:
    """전체 기록 로드"""
    _ensure_journal_file()
    try:
        with open(JOURNAL_FILE, "r", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    except Exception as e:
        logger.error(f"일지 로드 실패: {e}")
        return []


def get_open_trades() -> list[dict]:
    """현재 OPEN 상태 거래 목록"""
    return [r for r in load_all_records() if r.get("status") == "OPEN"]


def get_closed_trades() -> list[dict]:
    """청산 완료 거래 목록"""
    return [r for r in load_all_records() if r.get("status") == "CLOSED"]


# ══════════════════════════════════════════════════════════════
# 통계
# ══════════════════════════════════════════════════════════════

def update_stats() -> dict:
    """통계 계산 및 JSON 저장"""
    closed = get_closed_trades()
    stats = _calculate_stats(closed)
    try:
        with open(STATS_FILE, "w", encoding="utf-8") as f:
            json.dump(stats, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"통계 저장 실패: {e}")
    return stats


def _calculate_stats(closed: list[dict]) -> dict:
    """청산 기록에서 통계 계산"""
    if not closed:
        return {
            "total_trades": 0, "win": 0, "loss": 0, "win_rate": 0,
            "total_pnl": 0, "avg_pnl": 0, "max_win": 0, "max_loss": 0,
            "profit_factor": 0, "avg_rr": 0,
            "wave_stats": {}, "prg_win_rate": 0,
            "rsi_win_rate": 0, "both_win_rate": 0,
        }

    pnls    = [float(r["pnl_usdt"]) for r in closed]
    wins    = [p for p in pnls if p > 0]
    losses  = [p for p in pnls if p <= 0]

    total_win  = sum(wins)
    total_loss = abs(sum(losses))
    profit_factor = total_win / total_loss if total_loss > 0 else float("inf")

    # 손익비 (평균 이익 / 평균 손실)
    avg_win  = total_win  / len(wins)   if wins   else 0
    avg_loss = total_loss / len(losses) if losses else 1
    avg_rr   = avg_win / avg_loss if avg_loss > 0 else 0

    # 파동별 승률
    wave_stats = {}
    for r in closed:
        wp = r.get("wave_position", "unknown")
        if wp not in wave_stats:
            wave_stats[wp] = {"win": 0, "loss": 0}
        if float(r["pnl_usdt"]) > 0:
            wave_stats[wp]["win"] += 1
        else:
            wave_stats[wp]["loss"] += 1

    # PRG/RSI 확인 시 승률
    prg_trades  = [r for r in closed if r.get("prg_confirmed") in ("True", True)]
    rsi_trades  = [r for r in closed if r.get("rsi_confirmed") in ("True", True)]
    both_trades = [r for r in closed
                   if r.get("prg_confirmed") in ("True", True)
                   and r.get("rsi_confirmed") in ("True", True)]

    def _wr(lst):
        if not lst: return 0
        w = sum(1 for r in lst if float(r["pnl_usdt"]) > 0)
        return round(w / len(lst) * 100, 1)

    return {
        "total_trades"  : len(closed),
        "win"           : len(wins),
        "loss"          : len(losses),
        "win_rate"      : round(len(wins) / len(closed) * 100, 1),
        "total_pnl"     : round(sum(pnls), 4),
        "avg_pnl"       : round(sum(pnls) / len(pnls), 4),
        "max_win"       : round(max(pnls), 4),
        "max_loss"      : round(min(pnls), 4),
        "profit_factor" : round(profit_factor, 2),
        "avg_rr"        : round(avg_rr, 2),
        "wave_stats"    : wave_stats,
        "prg_win_rate"  : _wr(prg_trades),
        "rsi_win_rate"  : _wr(rsi_trades),
        "both_win_rate" : _wr(both_trades),
        "updated_at"    : datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def print_stats():
    """터미널에 통계 출력"""
    closed = get_closed_trades()
    open_t = get_open_trades()
    stats  = _calculate_stats(closed)

    border = "═" * 55
    print(f"\n{border}")
    print(f"  매매일지 통계 ({stats.get('updated_at', '-')})")
    print(f"{border}")
    print(f"  총 거래    : {stats['total_trades']}건  (오픈: {len(open_t)}건)")
    print(f"  승/패      : {stats['win']}승 / {stats['loss']}패")
    print(f"  승률       : {stats['win_rate']}%")
    print(f"  누적 손익  : {stats['total_pnl']:+.4f} USDT")
    print(f"  평균 손익  : {stats['avg_pnl']:+.4f} USDT")
    print(f"  최대 수익  : {stats['max_win']:+.4f} USDT")
    print(f"  최대 손실  : {stats['max_loss']:+.4f} USDT")
    print(f"  수익 팩터  : {stats['profit_factor']}")
    print(f"  평균 손익비: 1 : {stats['avg_rr']}")
    print(f"{border}")
    print(f"  [신호 유형별 승률]")
    print(f"  PRG 확인 시       : {stats['prg_win_rate']}%")
    print(f"  RSI 다이버전스 시 : {stats['rsi_win_rate']}%")
    print(f"  PRG + RSI 동시    : {stats['both_win_rate']}%  (10x 레버리지)")

    if stats.get("wave_stats"):
        print(f"  [파동 위치별 승률]")
        for wp, ws in stats["wave_stats"].items():
            total_w = ws["win"] + ws["loss"]
            wr = round(ws["win"] / total_w * 100, 1) if total_w else 0
            print(f"  {wp:>10s}: {wr}%  ({ws['win']}승/{ws['loss']}패)")

    print(f"{border}\n")
