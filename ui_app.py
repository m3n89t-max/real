"""
NEoWave 자동매매 봇 로컬 대시보드

실행: python ui_app.py
접속: http://localhost:5000
"""

import json
import os
from datetime import datetime
from flask import Flask, render_template, jsonify, request
from journal import (
    load_all_records, get_open_trades, get_closed_trades,
    _calculate_stats, DETAIL_FILE, STATS_FILE
)
from rule_loader import R, RULES_FILE, GUIDE_FILE

GUIDEBOOK_FILE = GUIDE_FILE
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trading_bot.log")

app = Flask(__name__)


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


# ══════════════════════════════════════════════════════════════
# 라우트
# ══════════════════════════════════════════════════════════════

@app.route("/")
def index():
    records  = load_all_records()
    stats    = _load_stats()
    details  = _load_details()
    open_pos = get_open_trades()
    closed   = get_closed_trades()

    # 각 record에 detail 병합
    enriched = []
    for r in reversed(records):   # 최신순
        d = details.get(r["trade_id"], {})
        enriched.append({**r, **d})

    return render_template(
        "index.html",
        records   = enriched,
        stats     = stats,
        open_pos  = open_pos,
        closed    = closed,
        now       = datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )


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


@app.route("/api/detail/<trade_id>")
def api_detail(trade_id):
    details = _load_details()
    d = details.get(trade_id, {})
    return jsonify(d)


@app.route("/api/guidebook")
def api_guidebook():
    try:
        content = R.get_guidebook()
        if not content:
            # 직접 경로 시도 (폴백)
            import pathlib
            direct = pathlib.Path(__file__).resolve().parent / "지침서.md"
            if direct.exists():
                content = direct.read_text(encoding="utf-8")
        if not content:
            return jsonify({"content": "지침서.md 파일을 찾을 수 없습니다.\n\n"
                            f"탐색 경로: {GUIDEBOOK_FILE}", "found": False})
        return jsonify({"content": content, "found": True})
    except Exception as e:
        return jsonify({"content": f"오류: {e}", "found": False})


@app.route("/api/guidebook/save", methods=["POST"])
def api_guidebook_save():
    """지침서.md 내용 저장 — 봇에 즉시 반영됨."""
    try:
        data = request.get_json(force=True)
        content = data.get("content", "")
        if not content.strip():
            return jsonify({"ok": False, "msg": "내용이 비어 있습니다."}), 400
        R.save_guidebook(content)
        return jsonify({"ok": True, "msg": "지침서가 저장되었습니다."})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/rules")
def api_rules():
    """현재 rules_config.json 전체 반환."""
    return jsonify(R.get_raw())


@app.route("/api/logs")
def api_logs():
    """로그 파일 폴링 — offset 이후 새 줄만 반환"""
    n      = int(request.args.get("n", 200))
    offset = int(request.args.get("offset", 0))   # 클라이언트가 마지막으로 받은 줄 번호
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
        return jsonify({"lines": [f"로그 읽기 오류: {e}"], "total": 0})


@app.route("/api/rules/save", methods=["POST"])
def api_rules_save():
    """rules_config.json 저장 → 봇 파라미터 즉시 반영."""
    try:
        data = request.get_json(force=True)
        R.save_rules(data)
        return jsonify({"ok": True, "msg": "파라미터가 저장되었습니다. 봇에 즉시 반영됩니다."})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


if __name__ == "__main__":
    print("\n" + "="*55)
    print("  NEoWave 자동매매 대시보드")
    print("  접속 주소: http://localhost:5000")
    print("="*55 + "\n")
    app.run(host="0.0.0.0", port=5000, debug=False)
