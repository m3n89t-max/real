import ccxt
import pandas as pd
import logging
from config import (
    BINANCE_API_KEY, BINANCE_SECRET_KEY,
    TF_4H, TF_1H, TF_15M, TF_5M, TF_1M,
    TF_LIMITS, LEVERAGE_DEFAULT, SYMBOLS
)

logger = logging.getLogger(__name__)


def create_exchange() -> ccxt.binance:
    exchange = ccxt.binance({
        "apiKey": BINANCE_API_KEY,
        "secret": BINANCE_SECRET_KEY,
        "options": {"defaultType": "future"},
        "enableRateLimit": True,
    })
    return exchange


def fetch_ohlcv(exchange, symbol: str, timeframe: str, limit: int) -> pd.DataFrame:
    """OHLCV 캔들 데이터 수집 → DataFrame 반환"""
    try:
        raw = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        df.set_index("timestamp", inplace=True)
        df = df.astype(float)
        return df
    except Exception as e:
        logger.error(f"[{symbol}] {timeframe} 수집 실패: {e}")
        return pd.DataFrame()


def fetch_all_timeframes(exchange, symbol: str) -> dict:
    """
    5개 타임프레임 데이터 수집

    반환: {
        "4h":  DataFrame,
        "1h":  DataFrame,
        "15m": DataFrame,
        "5m":  DataFrame,
        "1m":  DataFrame,
    }
    """
    result = {}
    timeframes = [TF_4H, TF_1H, TF_15M, TF_5M, TF_1M]

    for tf in timeframes:
        df = fetch_ohlcv(exchange, symbol, tf, TF_LIMITS[tf])
        if df.empty:
            logger.warning(f"[{symbol}] {tf} 데이터 없음")
            return {}
        result[tf] = df

    logger.info(
        f"[{symbol}] 수집 완료 | "
        + " | ".join(f"{tf}:{len(result[tf])}봉" for tf in timeframes)
    )
    return result


def fetch_entry_timeframes(exchange, symbol: str) -> dict:
    """
    5m + 1m 데이터만 빠르게 수집 (1m 타점 갱신용)
    """
    result = {}
    for tf in [TF_5M, TF_1M]:
        df = fetch_ohlcv(exchange, symbol, tf, TF_LIMITS[tf])
        if df.empty:
            return {}
        result[tf] = df
    return result


def setup_symbol(exchange, symbol: str):
    """레버리지 및 마진 모드 초기화"""
    try:
        exchange.set_margin_mode("isolated", symbol)
    except Exception:
        pass
    try:
        exchange.set_leverage(LEVERAGE_DEFAULT, symbol)
        logger.info(f"[{symbol}] 레버리지 {LEVERAGE_DEFAULT}x / isolated 마진 설정")
    except Exception as e:
        # 잔고 부족 등으로 레버리지 설정 실패해도 스캔/분석은 계속 진행
        logger.warning(f"[{symbol}] 레버리지 초기 설정 실패 (진입 시 재설정): {e}")


def set_leverage_dynamic(exchange, symbol: str, leverage: int):
    """동적 레버리지 변경 (진입 직전 호출)"""
    try:
        exchange.set_leverage(leverage, symbol)
        logger.info(f"[{symbol}] 레버리지 → {leverage}x")
    except Exception as e:
        logger.error(f"[{symbol}] 레버리지 변경 실패: {e}")


def get_account_balance(exchange) -> float:
    try:
        balance = exchange.fetch_balance({"type": "future"})
        usdt = float(balance["USDT"]["free"])
        logger.info(f"USDT 잔고: {usdt:.2f}")
        return usdt
    except Exception as e:
        logger.error(f"잔고 조회 실패: {e}")
        return 0.0


def get_open_positions(exchange) -> list:
    try:
        positions = exchange.fetch_positions(SYMBOLS)
        return [p for p in positions if float(p.get("contracts", 0)) != 0]
    except Exception as e:
        logger.error(f"포지션 조회 실패: {e}")
        return []


def get_current_price(exchange, symbol: str) -> float:
    try:
        ticker = exchange.fetch_ticker(symbol)
        return float(ticker["last"])
    except Exception as e:
        logger.error(f"[{symbol}] 현재가 조회 실패: {e}")
        return 0.0


def initialize_exchange() -> ccxt.binance:
    exchange = create_exchange()
    exchange.load_markets()
    logger.info("바이낸스 선물 마켓 로드 완료")
    for symbol in SYMBOLS:
        setup_symbol(exchange, symbol)
    return exchange
