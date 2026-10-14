"""BybitExchange 조회 반복이 반드시 끝나는지 (10/13 실계좌에서 체결 내역 조회가 끝없이 돌던 버그 재현·방지)"""
import app.exchange_bybit as XB
from app.exchange_bybit import BybitExchange


class FakeRest:
    def __init__(self):
        self.calls = 0

    def call(self, method, path, params=None):
        self.calls += 1
        if self.calls > 500:
            raise AssertionError("끝나지 않는 반복")
        if path == "/v5/execution/list":
            return {"list": [], "nextPageCursor": ""}
        if path == "/v5/order/realtime":
            return {"list": [], "nextPageCursor": "always-more"}       # 커서가 끝없이 와도 멈춰야 함
        return {}


def make(monkeypatch, clock):
    ex = BybitExchange.__new__(BybitExchange)
    ex.mode, ex.m = "live", None
    ex.rest = FakeRest()
    monkeypatch.setattr(XB, "now_ms", clock)
    return ex


def test_executions_terminates_even_when_clock_moves(monkeypatch):
    t = [1_792_000_000_000]

    def clock():
        t[0] += 50                                                      # 호출할 때마다 시간이 흐름
        return t[0]
    ex = make(monkeypatch, clock)
    assert ex.executions(t[0] - 60_000) == []
    assert ex.rest.calls <= 4


def test_open_orders_page_cap(monkeypatch):
    ex = make(monkeypatch, lambda: 1_792_000_000_000)
    assert ex.open_orders() == []
    assert ex.rest.calls == 50
