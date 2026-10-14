"""
전략 정의 (눌림목H 사전 고정 규칙 그대로 + 사용자 결정 손절 −15%). 진입은 셋이 같고 청산만 다르다.
공통 진입: 스크리너 S1 사건 중 cats.H3(= B·Q 거르기 통과) → 신호 종가 × 0.95 지정가(사이트 limit_px), limit_until 까지 유효.
- H3: 손절 −15% (거래소), 익절 없음, 체결 후 72시간에 시장가 청산.
- H4: 두 다리로 나눔. a = 절반, +12% 익절(거래소)·손절 −15%. b = 절반, 손절 −15%; a 가 익절되면 b 손절을 +0.5% 로 올림. 168시간 청산.
- H5: 손절 −15%; 체결 후 가격이 +5% 에 닿으면 손절을 +0.5% 로 올림. 168시간 청산.
사이트 리플레이 결과 키 (h_plan lim.final): H3 → H72, H4 → PT12_168, H5 → LK5_168.
"""

SITE_KEY = {"H3": "H72", "H4": "PT12_168", "H5": "LK5_168"}

# 화면 표시용 (1단계 순위표 순서)
INFO = {"H3": ("1위", "지정가 −5% 진입 · 72시간 뒤 청산"),
        "H4": ("2위", "+12%에서 절반 익절 · 나머지 본전+ · 168시간"),
        "H5": ("3위", "+5% 닿으면 손절을 본전+로 · 168시간")}

# 최소 예산 (USDT). tools/min_budget.py (2026-10-13): 실제 S1 사건 코인 60개 중 95% 를
# '최소 주문 크기 × 손절 15% ≤ 예산의 0.5%' 로 받을 수 있는 선 = H3·H5 172, H4(두 다리) 344 → 올림.
# 이보다 작은 예산은 그 방법을 끈다 (0 = 안 씀). 이 위로는 config.yaml 에서 사용자가 자유롭게.
MIN_BUDGET = {"H3": 180, "H4": 350, "H5": 180}


def legs_spec(strategy, scfg):
    """[(다리 이름, 수량 비율, 익절 % 또는 None)]"""
    if strategy == "H3":
        return [("m", 1.0, None)]
    if strategy == "H4":
        return [("a", 0.5, scfg["h4_take_profit_pct"]), ("b", 0.5, None)]
    if strategy == "H5":
        return [("m", 1.0, None)]
    raise ValueError(strategy)


def hold_ms(strategy, scfg):
    return int(scfg["hold_hours"][strategy] * 3_600_000)


def eligible(event) -> tuple[bool, str]:
    """이 사건이 H3·H4·H5 진입 대상인지 (사이트 판정 그대로 따름)"""
    cats = event.get("cats") or {}
    if not cats.get("H3"):
        return False, "B·Q 거르기 미통과 (사이트 H3 아님)"
    if not event.get("limit_px") or not event.get("limit_until"):
        return False, "지정가 정보 없음"
    if event.get("delisted"):
        return False, "상장 폐지"
    return True, "ok"


def link_id(mode, strategy, leg, role, symbol, bar_ms, attempt=0):
    """orderLinkId (최대 36자, 영문·숫자·-·_). 같은 사건·전략·다리·역할 = 같은 값 → 거래소가 중복 주문을 거부"""
    m = {"paper": "P", "demo": "D", "live": "L"}[mode]
    b = _b36(bar_ms // 60000)
    a = "" if not attempt else str(attempt)
    return f"AT{m}{strategy[1]}{leg}{role}{a}-{symbol[:14]}-{b}"[:36]


def _b36(n):
    s = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while n:
        n, r = divmod(n, 36)
        out = s[r] + out
    return out or "0"


def is_bot_link(link):
    return bool(link) and link.startswith("AT")
