"""시간 도우미: KST 날짜, 스크리너 캡처 시간대."""
import time
from datetime import datetime, timezone, timedelta

KST = timezone(timedelta(hours=9))
MIN = 60_000
H1 = 3_600_000
DAY = 86_400_000

# 스크리너 캡처 창 (KST). 이 시간엔 시세 요청을 줄인다 (주문·손절 관리는 계속).
CAPTURE_WINDOWS = ((12 * 60 + 55, 13 * 60 + 35), (20 * 60 + 55, 21 * 60 + 35), (23 * 60 + 55, 24 * 60), (0, 35))


def now_ms() -> int:
    return int(time.time() * 1000)


def kst_day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, KST).strftime("%Y-%m-%d")


def kst_str(ms) -> str:
    if not ms:
        return ""
    return datetime.fromtimestamp(ms / 1000, KST).strftime("%m-%d %H:%M:%S")


def in_capture(ms: int | None = None) -> bool:
    d = datetime.fromtimestamp((ms or now_ms()) / 1000, KST)
    m = d.hour * 60 + d.minute
    return any(a <= m < b for a, b in CAPTURE_WINDOWS)
