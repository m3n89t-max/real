"""
rule_loader.py
──────────────
rules_config.json과 지침서.md를 감시하여 변경 시 자동 재로드.
봇의 모든 모듈은 이 모듈을 통해 파라미터를 읽는다.

사용법:
    from rule_loader import R   # R.get("진입_조건.min_confidence") 등
"""

from __future__ import annotations
import json
import os
import logging
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_BASE_DIR  = Path(__file__).resolve().parent
RULES_FILE = str(_BASE_DIR / "rules_config.json")
GUIDE_FILE = str(_BASE_DIR / "지침서.md")


def _flatten(d: dict, prefix: str = "") -> dict:
    """중첩 딕셔너리를 '섹션.키' 형태로 평탄화."""
    out = {}
    for k, v in d.items():
        if k.startswith("_"):       # 주석 키 제외
            continue
        full_key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            out.update(_flatten(v, full_key))
        else:
            out[full_key] = v
    return out


class RuleLoader:
    """rules_config.json 파일을 주기적으로 감시·재로드한다."""

    def __init__(self):
        self._rules:   dict = {}
        self._flat:    dict = {}
        self._mtime_rules: float = 0.0
        self._mtime_guide: float = 0.0
        self._lock = threading.Lock()
        self._reload()             # 최초 로드

    # ── 로드 ─────────────────────────────────────────────────────────

    def _reload(self):
        try:
            with open(RULES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            with self._lock:
                self._rules = data
                self._flat  = _flatten(data)
                self._mtime_rules = os.path.getmtime(RULES_FILE)
            logger.info("[RuleLoader] rules_config.json 재로드 완료")
        except Exception as e:
            logger.error(f"[RuleLoader] 로드 실패: {e}")

    def check_and_reload(self):
        """봇 루프마다 호출 — 파일이 변경되면 자동 재로드."""
        try:
            mtime = os.path.getmtime(RULES_FILE)
            if mtime > self._mtime_rules:
                logger.info("[RuleLoader] rules_config.json 변경 감지 → 재로드")
                self._reload()
                return True
        except Exception:
            pass
        return False

    # ── 파라미터 조회 ──────────────────────────────────────────────

    def get(self, key: str, default: Any = None) -> Any:
        """
        '섹션.키' 형식으로 값 반환.
        예) R.get("진입_조건.min_confidence")
            R.get("레버리지.max")
        """
        with self._lock:
            return self._flat.get(key, default)

    def get_raw(self) -> dict:
        """전체 raw dict 반환 (대시보드 JSON 직렬화용)."""
        with self._lock:
            return dict(self._rules)

    # ── 지침서.md ─────────────────────────────────────────────────

    def get_guidebook(self) -> str:
        guide = Path(GUIDE_FILE)
        # 한국어 파일명 대체 경로 탐색
        if not guide.exists():
            for candidate in guide.parent.glob("*.md"):
                if "지침" in candidate.name or "guidebook" in candidate.name.lower():
                    guide = candidate
                    break
        try:
            return guide.read_text(encoding="utf-8")
        except Exception as e:
            logger.error(f"[RuleLoader] 지침서 읽기 실패: {e}")
            return ""

    def save_guidebook(self, content: str):
        Path(GUIDE_FILE).write_text(content, encoding="utf-8")
        logger.info("[RuleLoader] 지침서.md 저장 완료")

    # ── rules_config.json 저장 ──────────────────────────────────

    def save_rules(self, data: dict):
        """대시보드에서 수정한 JSON을 저장하고 즉시 재로드."""
        import datetime
        data["_updated"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(RULES_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        self._reload()
        logger.info("[RuleLoader] rules_config.json 저장 완료")

    # ── 자주 쓰는 파라미터 단축 프로퍼티 ────────────────────────

    @property
    def w2_max_retracement(self) -> float:
        return self.get("파동_절대법칙.wave2_max_retracement", 0.618)

    @property
    def extension_law_ratio(self) -> float:
        return self.get("파동_절대법칙.extension_law_ratio", 1.618)

    @property
    def wave4_overlap_allowed(self) -> bool:
        return self.get("파동_절대법칙.wave4_overlap_allowed", False)

    @property
    def wave3_shortest_reject(self) -> bool:
        return self.get("파동_절대법칙.wave3_shortest_reject", True)

    @property
    def fib_tolerance(self) -> float:
        return self.get("피보나치_비율.fib_tolerance", 0.12)

    @property
    def min_confidence(self) -> float:
        return self.get("진입_조건.min_confidence", 0.50)

    @property
    def counter_trend_min_conf(self) -> float:
        return self.get("진입_조건.counter_trend_min_conf", 0.75)

    @property
    def min_rr_ratio(self) -> float:
        return self.get("진입_조건.min_rr_ratio", 1.5)

    @property
    def min_prg_confluence(self) -> int:
        return int(self.get("진입_조건.min_prg_confluence", 2))

    @property
    def prg_zone_buffer_pct(self) -> float:
        return self.get("진입_조건.prg_zone_buffer_pct", 0.005)

    @property
    def somonics_tp_ratio(self) -> float:
        return self.get("목표가_손절_규칙.somonics_tp_ratio", 0.618)

    @property
    def stop_loss_pct(self) -> float:
        return self.get("목표가_손절_규칙.stop_loss_pct", 0.02)

    @property
    def account_risk_pct(self) -> float:
        return self.get("목표가_손절_규칙.account_risk_pct", 0.02)

    @property
    def min_tp_pct(self) -> float:
        return self.get("목표가_손절_규칙.min_tp_pct", 0.015)

    @property
    def max_tp_pct(self) -> float:
        return self.get("목표가_손절_규칙.max_tp_pct", 0.08)

    @property
    def leverage_default(self) -> int:
        return int(self.get("레버리지.default", 4))

    @property
    def leverage_medium(self) -> int:
        return int(self.get("레버리지.medium", 7))

    @property
    def leverage_max(self) -> int:
        return int(self.get("레버리지.max", 10))

    @property
    def pivot_left(self) -> int:
        return int(self.get("피벗_감지.pivot_left", 5))

    @property
    def pivot_right(self) -> int:
        return int(self.get("피벗_감지.pivot_right", 5))

    @property
    def scan_interval_sec(self) -> int:
        return int(self.get("스캔.scan_interval_sec", 30))

    @property
    def max_open_positions(self) -> int:
        return int(self.get("스캔.max_open_positions", 4))

    # ── 파동 피보나치 리스트 ──────────────────────────────────────

    def wave2_retracement(self) -> list:
        return self.get("피보나치_비율.wave2_retracement", [0.382, 0.500, 0.618])

    def wave3_extension(self) -> list:
        return self.get("피보나치_비율.wave3_extension", [1.272, 1.618, 2.618])

    def wave5_extension(self) -> list:
        return self.get("피보나치_비율.wave5_extension", [0.618, 1.000, 1.272])

    def zigzag_c_ratios(self) -> list:
        return self.get("조정파동_규칙.zigzag_c_ratios", [0.618, 1.000, 1.272, 1.618])

    def flat_c_ratios(self) -> list:
        return self.get("조정파동_규칙.flat_c_ratios", [1.000, 1.272, 1.618])


# ── 전역 싱글턴 ──────────────────────────────────────────────────────
R = RuleLoader()
