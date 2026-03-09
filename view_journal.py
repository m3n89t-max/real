"""
매매일지 조회 스크립트

사용법:
  python view_journal.py           # 전체 통계 + 최근 20건
  python view_journal.py --open    # 현재 오픈 포지션만
  python view_journal.py --all     # 전체 내역
  python view_journal.py --win     # 수익 거래만
  python view_journal.py --loss    # 손실 거래만
  python view_journal.py --symbol XRP/USDT  # 특정 코인만
"""

import sys
import csv
from journal import (
    load_all_records, get_open_trades, get_closed_trades,
    print_stats, JOURNAL_FILE
)


def fmt_pnl(val: str) -> str:
    try:
        v = float(val)
        return f"{v:+.4f}" if v != 0 else "  -"
    except Exception:
        return val


def print_records(records: list[dict], title: str = "매매 내역"):
    if not records:
        print(f"\n  [{title}] 기록 없음\n")
        return

    print(f"\n  [{title}]  총 {len(records)}건")
    print(f"  {'ID':>6}  {'시간':>19}  {'코인':>10}  {'방향':>5}  "
          f"{'진입가':>12}  {'청산가':>12}  {'PnL(U)':>10}  {'PnL(%)':>8}  "
          f"{'사유':>6}  {'파동':>8}  {'LV':>3}")
    print("  " + "─" * 115)

    for r in records:
        side_mark = "▲ L" if r["side"] == "long" else "▼ S"
        exit_p    = r["exit_price"] if r["status"] == "CLOSED" else "OPEN"
        pnl_u     = fmt_pnl(r["pnl_usdt"]) if r["status"] == "CLOSED" else " -"
        pnl_p     = f"{float(r['pnl_pct']):+.2f}%" if r["status"] == "CLOSED" and r["pnl_pct"] else " -"
        reason    = r.get("exit_reason", "-") or "-"
        wave      = r.get("wave_position", "-")
        lv        = r.get("leverage", "-")
        time_str  = r.get("entry_time", "")[:19]

        print(f"  {r['trade_id']:>6}  {time_str:>19}  {r['symbol']:>10}  {side_mark:>5}  "
              f"{float(r['entry_price']):>12.4f}  {str(exit_p):>12}  {pnl_u:>10}  "
              f"{pnl_p:>8}  {reason:>6}  {wave:>8}  {lv:>3}")

    print()


def main():
    args = sys.argv[1:]

    # 통계는 항상 출력
    print_stats()

    if "--open" in args:
        print_records(get_open_trades(), "오픈 포지션")

    elif "--win" in args:
        closed = get_closed_trades()
        wins   = [r for r in closed if float(r.get("pnl_usdt", 0)) > 0]
        print_records(wins, "수익 거래")

    elif "--loss" in args:
        closed = get_closed_trades()
        losses = [r for r in closed if float(r.get("pnl_usdt", 0)) <= 0]
        print_records(losses, "손실 거래")

    elif "--symbol" in args:
        idx = args.index("--symbol")
        if idx + 1 < len(args):
            sym     = args[idx + 1].upper()
            records = [r for r in load_all_records() if r["symbol"] == sym]
            print_records(records, f"{sym} 전체 내역")

    elif "--all" in args:
        print_records(load_all_records(), "전체 내역")

    else:
        # 기본: 최근 20건
        recent = load_all_records()[-20:]
        print_records(recent, "최근 20건")


if __name__ == "__main__":
    main()
