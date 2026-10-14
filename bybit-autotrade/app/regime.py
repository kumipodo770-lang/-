"""
레짐 라벨 (regime_study.md 정의 그대로, 매일 04:00 UTC = 13:00 KST 판정, 그 시각까지 끝난 봉만).
 K1: BTC 일봉 종가 vs SMA50, SMA50 − 10일 전 SMA50 → +1 상승 / 0 횡보 / −1 하락
 K7: BTC 30일 실현변동성(일 로그수익 표준편차 × √365) 의 직전 365일 백분위 → >67 고(+1) / 중(0) / <33 저(−1)
데이터: 바이비트 BTCUSDT 무기한 1시간봉 (연구는 바이낸스 현물 1시간봉 — 값이 거의 같지만 같지는 않음, 기록에 출처 남김).
"""
import math
import logging
from .timeutil import H1, DAY, now_ms

log = logging.getLogger("regime")
CUT = 4 * H1


def daily_from_1h(bars, now=None):
    """[(t,o,h,l,c)] 1시간봉 → 04:00 UTC 마감 일봉 [(d, c)] (시간봉 20개 이상, 마감 시각 d ≤ now 인 끝난 날만)"""
    now = now or now_ms()
    days = {}
    for t, o, h, l, c in bars:
        end = t + H1
        d = ((end - CUT + DAY - 1) // DAY) * DAY + CUT
        x = days.setdefault(d, [0, None, None])
        x[0] += 1
        if x[1] is None or t > x[1]:
            x[1], x[2] = t, c
    return [(d, v[2]) for d, v in sorted(days.items()) if v[0] >= 20 and d <= now]


def labels(daily):
    """마지막 날 기준 (K1, K7, 설명 dict)"""
    c = [x[1] for x in daily]
    n = len(c)
    if n < 60:
        return None, None, {"err": "일봉 부족"}
    sma = [None] * n
    for i in range(49, n):
        sma[i] = sum(c[i - 49:i + 1]) / 50
    i = n - 1
    s, s10 = sma[i], sma[i - 10] if i - 10 >= 49 else None
    k1 = None
    if s is not None and s10 is not None:
        slope = s - s10
        k1 = 1 if (c[i] > s and slope > 0) else (-1 if (c[i] < s and slope < 0) else 0)
    lr = [math.nan] + [math.log(c[j] / c[j - 1]) for j in range(1, n)]
    rv = [math.nan] * n
    for j in range(30, n):
        w = lr[j - 29:j + 1]
        m = sum(w) / 30
        rv[j] = math.sqrt(sum((x - m) ** 2 for x in w) / 30) * math.sqrt(365) * 100
    win = [x for x in rv[max(0, i - 364):i + 1] if not math.isnan(x)]
    k7, pct, q33, q67 = None, None, None, None
    if len(win) >= 180 and not math.isnan(rv[i]):
        pct = (sum(x < rv[i] for x in win) / len(win)) * 100 + (sum(x == rv[i] for x in win) / len(win)) * 50
        k7 = 1 if pct > 67 else (-1 if pct < 33 else 0)
        sw = sorted(win)
        q33, q67 = sw[int(len(sw) * 0.33)], sw[int(len(sw) * 0.67)]
    return k1, k7, {"day": daily[-1][0], "close": c[i], "sma50": s, "sma50_10d_ago": s10, "rv30": rv[i], "rv_pct": pct,
                    "rv_q33": q33, "rv_q67": q67}


K1_TXT = {1: "상승", 0: "횡보", -1: "하락", None: "모름"}
K7_TXT = {1: "고변동", 0: "중변동", -1: "저변동", None: "모름"}


def explain(snap: dict) -> list[str]:
    """레짐 근거를 사람 말로 (화면·텔레그램 공용)"""
    out = []
    c, s, s10 = snap.get("close"), snap.get("sma50"), snap.get("sma50_10d_ago")
    if c and s and s10:
        side = "위" if c > s else "아래"
        slope = s - s10
        out.append(f"K1 {K1_TXT[snap.get('k1')]}: BTC 종가 {c:,.0f} 가 50일 평균 {s:,.0f} {side} ({(c / s - 1) * 100:+.1f}%), "
                   f"50일 평균이 10일 전보다 {slope:+,.0f} ({'오르는 중' if slope > 0 else '내리는 중'})")
    rv, p, q = snap.get("rv30"), snap.get("rv_pct"), snap.get("rv_q33")
    if rv and p is not None and q:
        out.append(f"변동성 {K7_TXT[snap.get('k7')]}: BTC 30일 변동성 연 {rv:.1f}% = 지난 1년 중 {p:.0f}백분위 "
                   f"(저변동 경계 연 {q:.1f}%, 고변동 경계 연 {snap.get('rv_q67'):.1f}%)")
    return out


class Regime:
    def __init__(self, market):
        self.m = market
        self.bars = []
        self.k1 = self.k7 = None
        self.info = {}
        self.updated = 0

    def refresh(self, force=False):
        now = now_ms()
        if not force and now - self.updated < 30 * 60 * 1000:
            return
        try:
            start = (self.bars[-1][0] + H1) if self.bars else now - 420 * DAY
            while start < now - H1:
                rows = self.m.klines("BTCUSDT", "60", start=start, end=min(now, start + 1000 * H1), limit=1000)
                rows = [r for r in rows if r[0] + H1 <= now]      # 끝난 봉만
                if not rows:
                    break
                known = {b[0] for b in self.bars}
                self.bars += [r for r in rows if r[0] not in known]
                start = rows[-1][0] + H1
            self.bars = sorted(self.bars)[-int(430 * 24):]
            self.k1, self.k7, self.info = labels(daily_from_1h(self.bars))
            self.info["source"] = "bybit BTCUSDT linear 1h"
            self.updated = now
        except Exception as e:
            log.warning("레짐 갱신 실패: %s", e)

    def snapshot(self):
        s = {"k1": self.k1, "k7": self.k7, **self.info}
        s["k1_txt"], s["k7_txt"] = K1_TXT.get(self.k1, "모름"), K7_TXT.get(self.k7, "모름")
        s["explain"] = explain(s)
        return s
