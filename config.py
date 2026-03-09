import os
from dotenv import load_dotenv

load_dotenv()

# ─── 바이낸스 API ──────────────────────────────────────────────
BINANCE_API_KEY    = os.getenv("BINANCE_API_KEY")
BINANCE_SECRET_KEY = os.getenv("BINANCE_SECRET_KEY")

# ─── 거래 대상 코인 (화이트리스트 8개) ────────────────────────
SYMBOLS = [
    "BTC/USDT",
    "ETH/USDT",
    "BNB/USDT",
    "SOL/USDT",
    "XRP/USDT",
    "DOGE/USDT",
    "LINK/USDT",
    "ARB/USDT",
]

# ─── 복합 타임프레임 설정 ─────────────────────────────────────
# 4h : 최상위 추세 방향 (Big Degree)
# 1h : 중기 파동 카운팅
# 15m: 세부 파동 확인
# 5m : 메인 매매 로직 (진입 구간)
# 1m : 정밀 진입 타점

TF_4H  = "4h"
TF_1H  = "1h"
TF_15M = "15m"
TF_5M  = "5m"
TF_1M  = "1m"

# 각 TF별 캔들 수집 개수
TF_LIMITS = {
    TF_4H:  200,
    TF_1H:  200,
    TF_15M: 200,
    TF_5M:  300,
    TF_1M:  200,
}

# ─── 레버리지 설정 ────────────────────────────────────────────
LEVERAGE_DEFAULT  = 4    # 기본 레버리지
LEVERAGE_MEDIUM   = 7    # PRG 또는 RSI 다이버전스 단독 확인
LEVERAGE_MAX      = 10   # PRG + RSI 다이버전스 동시 확인 (확실한 자리)

# ─── 리스크 관리 ──────────────────────────────────────────────
# 개별 포지션당 최대 허용 손실 = 계좌 잔고 × ACCOUNT_RISK_PCT
ACCOUNT_RISK_PCT  = 0.02  # 2%

# 동시 최대 오픈 포지션 수
MAX_OPEN_POSITIONS = 4

# ─── 손절 / 목표가 ────────────────────────────────────────────
STOP_LOSS_PCT        = 0.02   # 기본 손절 2% (포지션 크기 역산 기준)

# 소모닉 TP: 0.618 되돌림 고정 (지침서 엔진5 룰)
# 터미널 TP: 형성 시간 50% 내 시작점 100% 되돌림
# 삼각형 TP: 가장 긴 파동의 75%~100%
MIN_TAKE_PROFIT_PCT  = 0.015  # 최소 1.5%
MAX_TAKE_PROFIT_PCT  = 0.08   # 최대 8%

# ─── 파동 분석 파라미터 ───────────────────────────────────────
PIVOT_LEFT  = 5
PIVOT_RIGHT = 5
FIB_TOLERANCE = 0.12  # ±12% (지침서 기준 엄격 적용)

# 호가 양보 시스템 (슬리피지 버퍼, 지침서 엔진5)
# 현재가 구간별 양보 USDT
SLIPPAGE_BUFFERS = [
    (10_000,  20_000,  13),   # 10k~20k: $13
    (20_000,  30_000,  20),   # 20k~30k: $20
    (30_000,  50_000,  25),   # 30k~50k: $25
    (50_000, 100_000,  33),   # 50k~100k: $33
    (100_000, float("inf"), 50),
]
SLIPPAGE_DEFAULT_PCT = 0.0003  # 가격대 미해당 시 0.03%

# ─── PRG (Potential Reversal Zone) ───────────────────────────
PRG_MIN_CONFLUENCE = 2   # 최소 피보나치 중첩 개수

# ─── 스캔 설정 ────────────────────────────────────────────────
SCAN_INTERVAL = 30   # 30초마다 스캔 (5m/1m 기준)
LOG_FILE = "trading_bot.log"
