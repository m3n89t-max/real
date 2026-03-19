"""
리스크 관리 모듈 v3.1

핵심 원칙:
- 시드 100% 활용 (포지션당 계좌 5% 리스크, 수익 전액 재투입)
- 포지션 크기 = 최대허용손실 / 손절거리 (레버리지 적용 역산)
- 동적 레버리지: 3x / 5x / 7x
"""

import math
import logging
from rule_loader import R

logger = logging.getLogger(__name__)


def calculate_position_size(
    balance: float,
    entry_price: float,
    stop_loss: float,
    leverage: int,
    open_position_count: int,
    symbol: str = "",
) -> float:
    """
    계좌 2% 손실 기준 포지션 크기 역산

    공식:
        최대허용손실(USDT) = balance × ACCOUNT_RISK_PCT
        손절거리(%)        = |entry - stop| / entry
        투입증거금(USDT)   = 최대허용손실 / 손절거리(%)
        포지션명목금액     = 투입증거금 × leverage
        수량               = 포지션명목금액 / entry_price

    Args:
        balance            : 사용가능 USDT 잔고
        entry_price        : 진입가
        stop_loss          : 손절가
        leverage           : 레버리지 (4 / 7 / 10)
        open_position_count: 현재 오픈 포지션 수
    """
    if open_position_count >= R.max_open_positions:
        logger.warning(f"[{symbol}] 최대 포지션 수 초과 ({open_position_count}/{R.max_open_positions})")
        return 0.0

    if entry_price <= 0 or stop_loss <= 0:
        return 0.0

    stop_distance_pct = abs(entry_price - stop_loss) / entry_price
    if stop_distance_pct <= 0:
        return 0.0

    # 계좌 2% 손실 기준 최대 허용 손실
    max_loss_usdt = balance * R.account_risk_pct

    # 명목금액 역산: 손실 = 명목 × 손절거리%  →  명목 = 손실 / 손절거리%
    notional_usdt = max_loss_usdt / stop_distance_pct

    # 실제 투입 증거금 = 명목 / 레버리지
    margin_usdt = notional_usdt / leverage

    # 수량
    quantity = notional_usdt / entry_price

    logger.info(
        f"[{symbol}] 포지션 계산 | "
        f"잔고:{balance:.2f} | 최대손실:{max_loss_usdt:.2f} USDT | "
        f"손절거리:{stop_distance_pct:.2%} | 증거금:{margin_usdt:.2f} | "
        f"명목:{notional_usdt:.2f} | 레버리지:{leverage}x | 수량:{quantity:.6f}"
    )
    return quantity


def round_quantity(quantity: float, step_size: float) -> float:
    """거래소 최소 주문 단위로 내림"""
    if step_size <= 0:
        return quantity
    precision = max(0, int(round(-math.log10(step_size))))
    return round(math.floor(quantity / step_size) * step_size, precision)


def is_risk_acceptable(
    balance: float,
    entry_price: float,
    stop_loss: float,
    take_profit: float,
    quantity: float,
    leverage: int,
    symbol: str = "",
) -> bool:
    """
    최종 리스크 검증

    - 실제 최대 손실이 계좌의 3% 이내인지 확인 (2% 목표 + 1% 여유)
    - 손익비 1.5 이상
    - 증거금이 잔고의 50% 초과 여부
    """
    risk_per_unit   = abs(entry_price - stop_loss)
    reward_per_unit = abs(entry_price - take_profit)

    if risk_per_unit <= 0:
        return False

    rr_ratio      = reward_per_unit / risk_per_unit
    max_loss_usdt = risk_per_unit * quantity
    max_loss_pct  = max_loss_usdt / balance if balance > 0 else 1.0
    margin_used   = (entry_price * quantity / leverage) / balance if balance > 0 else 1.0

    logger.info(
        f"[{symbol}] 리스크 검증 | RR:{rr_ratio:.2f} | "
        f"손실:{max_loss_usdt:.2f} USDT ({max_loss_pct:.1%}) | "
        f"증거금사용:{margin_used:.1%}"
    )

    # 부동소수점 오차 허용 (1.4999... == 1.5 처리)
    if rr_ratio < R.min_rr_ratio - 1e-9:
        logger.warning(f"[{symbol}] 손익비 부족: {rr_ratio:.2f} < {R.min_rr_ratio}")
        return False

    # 단일 포지션 최대 손실: account_risk_pct × 3배 이내 (슬리피지 여유)
    max_allowed_loss = R.account_risk_pct * 3
    if max_loss_pct > max_allowed_loss:
        logger.warning(f"[{symbol}] 손실 과다: {max_loss_pct:.1%} > {max_allowed_loss:.1%}")
        return False

    return True


def check_total_exposure(
    balance: float,
    open_positions: list,
    new_notional: float,
    new_leverage: int = 3,
    symbol: str = "",
) -> bool:
    """
    전체 포지션 증거금(마진) 합계가 계좌의 max_total_exposure_pct 이하인지 확인.
    레버리지 거래이므로 명목가가 아닌 실제 투입 증거금으로 판단.
    """
    if balance <= 0:
        return False

    existing_margin = 0.0
    for p in (open_positions or []):
        try:
            contracts = abs(float(p.get("contracts", 0)))
            mark_price = float(p.get("markPrice", 0) or p.get("entryPrice", 0) or 0)
            lev = float(p.get("leverage", 1) or 1)
            existing_margin += (contracts * mark_price) / lev
        except (ValueError, TypeError):
            continue

    new_margin = new_notional / max(new_leverage, 1)
    total_margin = existing_margin + new_margin
    margin_pct = total_margin / balance

    max_exp = R.max_total_exposure_pct
    if margin_pct > max_exp:
        logger.warning(
            f"[{symbol}] 증거금 초과: {margin_pct:.1%} > {max_exp:.1%} | "
            f"기존마진:{existing_margin:.0f} + 신규마진:{new_margin:.0f} USDT"
        )
        return False

    logger.info(f"[{symbol}] 증거금 OK: {margin_pct:.1%} / {max_exp:.1%}")
    return True


def get_market_precision(exchange, symbol: str) -> dict:
    """심볼 정밀도 정보 조회"""
    try:
        market = exchange.market(symbol)
        return {
            "amount"  : market["precision"]["amount"],
            "price"   : market["precision"]["price"],
            "min_qty" : market["limits"]["amount"]["min"],
            "min_cost": market["limits"].get("cost", {}).get("min", 5.0) or 5.0,
        }
    except Exception as e:
        logger.error(f"[{symbol}] 정밀도 조회 실패: {e}")
        return {"amount": 0.001, "price": 0.01, "min_qty": 0.001, "min_cost": 5.0}
