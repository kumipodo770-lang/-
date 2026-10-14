import os
import sys
import copy
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import config as C  # noqa: E402

MIN = 60_000
T0 = 1_792_000_000_000 // MIN * MIN      # 고정 시작 시각 (분 경계)


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t

    def adv(self, ms):
        self.t += ms


class FakeMarket:
    """1분봉을 직접 넣는 가짜 시세"""
    def __init__(self, clock):
        self.clock = clock
        self.bars = {}          # symbol → [(t,o,h,l,c)]
        self.price = {}
        self.inst = {"ABCUSDT": {"status": "Trading", "tick": 0.0001, "min_qty": 1.0, "qty_step": 1.0, "max_mkt_qty": 1e7,
                                 "min_notional": 5.0, "max_lev": 25.0, "funding_min": 480},
                     "XYZUSDT": {"status": "Trading", "tick": 0.001, "min_qty": 0.1, "qty_step": 0.1, "max_mkt_qty": 1e7,
                                 "min_notional": 5.0, "max_lev": 25.0, "funding_min": 480}}
        self.calls = 0

    def instruments(self, force=False):
        return self.inst

    def instrument(self, s):
        return self.inst.get(s)

    def ticker(self, s):
        self.calls += 1
        return {"last": self.price[s], "mark": self.price[s], "funding": 0.0, "next_funding": 0}

    def add_bar(self, s, o, h, l, c):
        """현재 시각이 속한 1분봉을 넣고 시계를 다음 분으로"""
        t = self.clock() // MIN * MIN
        self.bars.setdefault(s, []).append((t, o, h, l, c))
        self.price[s] = c
        self.clock.t = t + MIN + 1000

    def klines(self, s, interval="1", start=None, end=None, limit=1000):
        self.calls += 1
        rows = [b for b in self.bars.get(s, []) if (start is None or b[0] >= start) and (end is None or b[0] <= end)]
        return rows[-limit:]

    def funding_history(self, s, start=None, end=None, limit=50):
        return []

    def mmr(self, s):
        return 0.01


class FakeScreener:
    def __init__(self):
        self.events = []

    def hplan(self, days=2):
        return {"ready": True, "events": copy.deepcopy(self.events)}


class FakeRegime:
    def __init__(self, k1=1, k7=0):
        self.k1, self.k7 = k1, k7

    def refresh(self, force=False):
        pass

    def snapshot(self):
        return {"k1": self.k1, "k7": self.k7}


class FakeNotify:
    def __init__(self):
        self.msgs = []
        self.ok = True
        self.chat_id = "1"

    def send(self, t, buttons=None):
        self.msgs.append(t)


def make_event(symbol="ABCUSDT", t=None, c0=1.0526316, limit=1.0, until_h=24, status="wait"):
    t = t or T0
    return {"id": f"{symbol}-{t - 15 * MIN}", "symbol": symbol, "t_sig": t, "c0": c0, "q24": 1e7, "qrel": 2.0,
            "cats": {"H1": False, "H2": False, "H3": True, "H4": True, "H5": True}, "flags": {"W": True, "B": True, "Q": True, "R": False},
            "limit_px": limit, "limit_until": t + until_h * 3_600_000, "warmup": False, "done": False, "delisted": False,
            "lim": {"status": status, "final": {}}, "site": None}


@pytest.fixture
def cfg():
    c = C._merge(C.DEFAULTS, {})
    c["books"] = [{"name": "paper", "mode": "paper", "budgets": {"H3": 300, "H4": 300, "H5": 300},
                   "regime_gate": {"k1_down": "record", "low_vol": "record"}}]
    C.validate(c)
    return c
