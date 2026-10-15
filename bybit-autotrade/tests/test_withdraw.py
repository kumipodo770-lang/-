"""출금 (사용자 결정 2026-10-15): 화이트리스트 주소로만, 통장 금액 ≥ 요청, 하루 5회, 매수→잔고 확인→출금 순서, 실패 처리, 웹 흐름"""
from decimal import Decimal
import pytest
from fastapi.testclient import TestClient
from app import withdraw as W
from app.bybit_rest import BybitError, BybitNetError, WITHDRAW_ALLOWED
from app.store import Store
from app.web import create_app

ADDR, TAG = "rYmTPYVPLv4EbhTN4Uw8nfvjyShLGESMV", "2278992574"
WCFG = {"enabled": True, "coin": "XRP", "chain": "XRP", "address": ADDR, "tag": TAG, "max_usd": 100, "count_per_day": 5,
        "buffer_pct": 2.0, "upbit_fee_pct": 0.05, "upbit_krw_withdraw_fee": 1000, "spot_fee_pct": 0.1, "usd_krw": 0}
T0 = 1_792_000_000_000


class Resp:
    def __init__(self, d):
        self.d = d

    def raise_for_status(self):
        pass

    def json(self):
        return self.d


class FakeHTTP:
    """업비트·바이비트 공개 시세"""
    def __init__(self, xrp_krw=3500.0, usdt_krw=1400.0, xrp_usdt=2.5):
        self.xrp_krw, self.usdt_krw, self.xrp_usdt = xrp_krw, usdt_krw, xrp_usdt

    def get(self, url, params=None, timeout=None):
        if "upbit" in url:
            return Resp([{"market": "KRW-XRP", "trade_price": self.xrp_krw}, {"market": "KRW-USDT", "trade_price": self.usdt_krw}])
        if "tickers" in url:
            return Resp({"result": {"list": [{"ask1Price": str(self.xrp_usdt), "lastPrice": str(self.xrp_usdt)}]}})
        return Resp({"result": {"list": [{"lotSizeFilter": {"basePrecision": "0.01", "minOrderQty": "0.1", "minOrderAmt": "1"}}]}})


class FakeRest:
    def __init__(self, xrp=0.0, avail=400.0, fill=True, wd_error=None):
        self.calls, self.xrp, self.avail, self.fill, self.wd_error = [], xrp, avail, fill, wd_error

    def call(self, method, path, params=None):
        self.calls.append((method, path, dict(params or {})))
        if path == "/v5/asset/coin/query-info":
            return {"rows": [{"coin": "XRP", "chains": [{"chain": "XRP", "withdrawFee": "0.2", "withdrawMin": "1",
                                                         "minAccuracy": "6", "chainWithdraw": "1"}]}]}
        if path == "/v5/account/wallet-balance":
            return {"list": [{"totalAvailableBalance": str(self.avail), "coin": [{"coin": "XRP", "walletBalance": str(self.xrp)}]}]}
        if path == "/v5/order/create":
            if self.fill:
                self.xrp += float(params["qty"]) * (1 - 0.001)
            return {"orderId": "o1"}
        if path == "/v5/asset/withdraw/create":
            if self.wd_error:
                raise self.wd_error
            return {"id": "w123"}
        raise AssertionError(path)

    def paths(self):
        return [p for _, p, _ in self.calls]


class Book:
    def __init__(self):
        self.name, self.last_equity, self.raw_budgets = "live", 424.0, {"H3": 0.0, "H4": 350.0, "H5": 0.0}


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t


@pytest.fixture
def mk(tmp_path):
    def make(rest=None, http=None, **over):
        st, clock, msgs = Store(str(tmp_path / "w.db")), Clock(), []
        wd = W.Withdrawer({**WCFG, **over}, rest or FakeRest(), Book(), st, msgs.append, http=http or FakeHTTP(), clock=clock,
                          sleep=lambda s: None)
        return wd, st, clock, msgs
    return make


# ---------- 주소·설정 ----------
def test_address_checksum_and_config_check():
    assert W.valid_xrp_address(ADDR)
    assert not W.valid_xrp_address(ADDR[:-1] + "W") and not W.valid_xrp_address("") and not W.valid_xrp_address("0x1234")
    assert W.check_config(WCFG) is None
    for bad in ({"address": ""}, {"address": ADDR.replace("Y", "y")}, {"tag": "abc"}, {"tag": "4294967296"},
                {"count_per_day": 6}, {"count_per_day": 0}, {"max_usd": 0}, {"coin": "USDT"}, {"buffer_pct": 50}):
        assert W.check_config({**WCFG, **bad}), bad


# ---------- 계산 ----------
Q = {"usd_krw": 1400.0, "xrp_krw": 3500.0, "xrp_usdt": 2.5, "wd_fee": 0.2, "wd_min": 1.0, "wd_step": 1e-6, "have_xrp": 0.0,
     "avail_usd": 400.0, "spot_step": 0.01, "spot_min_qty": 0.1, "spot_min_amt": 1.0}


@pytest.mark.parametrize("usd", [1, 5, 10, 37.5, 100])
def test_bank_amount_never_below_request(usd):
    p = W.compute(usd, Q, WCFG)
    assert p["bank_krw"] >= p["target_krw"]                             # 지금 시세면 여유만큼 더 들어옴
    drop = Q["xrp_krw"] * (1 - 0.0196)                                  # 옮기는 사이 업비트 시세가 여유(2%)만큼 떨어져도
    worst = (float(p["amount"]) - Q["wd_fee"]) * drop * (1 - 0.0005) - 1000
    assert worst >= p["target_krw"] - 1e-6
    if Decimal(p["amount"]) > Decimal("1"):                             # 바이비트 최소 출금 수량에 걸리지 않으면
        assert p["bank_krw"] <= p["target_krw"] * 1.03 + 50             # 많아도 조금만 (최소 수량에 걸리면 그만큼 더 감)
    a = Decimal(p["amount"])
    assert a == a.quantize(Decimal("0.000001")) and a >= Decimal("1")


def test_buy_only_shortfall_and_respects_spot_minimums():
    p = W.compute(10, Q, WCFG)
    need = Decimal(p["need_bal"])
    assert need == Decimal(p["amount"]) + Decimal("0.2")
    assert Decimal(p["buy"]) * Decimal("0.999") >= need                 # 수수료 떼고도 출금액 + 수수료 이상
    assert W.compute(10, {**Q, "have_xrp": 100.0}, WCFG)["buy"] == "0"  # 이미 충분하면 안 삼
    small = W.compute(10, {**Q, "have_xrp": float(need) - 0.05}, WCFG)  # 부족분 0.05 → 최소 주문 맞춰 올림
    assert Decimal(small["buy"]) >= Decimal("0.4")                      # 최소 주문금액 1 USDT / 2.5 = 0.4


def test_withdraw_min_applies():
    p = W.compute(0.1, {**Q, "wd_min": 20.0}, WCFG)
    assert Decimal(p["amount"]) == Decimal("20")


# ---------- 미리 보기 ----------
def test_preview_limits_and_warnings(mk):
    wd, st, clock, msgs = mk()
    with pytest.raises(W.WithdrawError):
        wd.preview(101)
    with pytest.raises(W.WithdrawError):
        wd.preview(0)
    with pytest.raises(W.WithdrawError):
        wd.preview("abc")
    p = wd.preview(10)
    assert p["left_today"] == 5 and "rYmT" in p["dest"] and "2574" in p["dest"]
    assert p["warnings"] == []                                          # 잔고 424 − 매수 약 12 > 예산 350
    wd.book.last_equity = 355.0
    assert "예산 합계" in wd.preview(10)["warnings"][0]                 # 출금 뒤 잔고 < 예산 합계 → 경고 (막지는 않음)
    wd2, *_ = mk(rest=FakeRest(avail=1.0))
    with pytest.raises(W.WithdrawError, match="USDT 부족"):
        wd2.preview(10)


def test_preview_flags_computation_anomaly(mk):
    wd, *_ = mk(http=FakeHTTP(xrp_krw=35.0))                           # 업비트 시세가 비정상으로 낮게 오면 보낼 XRP 가 과대 → 거부
    with pytest.raises(W.WithdrawError, match="계산 이상"):
        wd.preview(10)


# ---------- 실행 ----------
def test_execute_buys_then_withdraws_to_config_address(mk):
    rest = FakeRest()
    wd, st, clock, msgs = mk(rest=rest)
    p = wd.preview(10)
    msg = wd.execute(p)
    assert "출금 접수" in msg and "w123" in msg and msgs[-1] == msg
    paths = rest.paths()
    i_buy, i_wd = paths.index("/v5/order/create"), paths.index("/v5/asset/withdraw/create")
    assert i_buy < i_wd and "/v5/account/wallet-balance" in paths[i_buy + 1:i_wd]
    buy = rest.calls[i_buy][2]
    assert buy["category"] == "spot" and buy["symbol"] == "XRPUSDT" and buy["side"] == "Buy" and buy["marketUnit"] == "baseCoin"
    wcall = rest.calls[i_wd][2]
    assert (wcall["coin"], wcall["chain"], wcall["address"], wcall["tag"]) == ("XRP", "XRP", ADDR, TAG)
    assert Decimal(wcall["amount"]) == Decimal(p["amount"])
    assert wd.used_today() == 1 and st.get("withdraw:log")[-1]["status"] == "ok"
    assert any(e["kind"] == "출금" for e in st.q("SELECT * FROM events"))


def test_no_buy_when_xrp_already_enough(mk):
    rest = FakeRest(xrp=100.0)
    wd, *_ = mk(rest=rest)
    wd.execute(wd.preview(10))
    assert "/v5/order/create" not in rest.paths() and "/v5/asset/withdraw/create" in rest.paths()


def test_daily_count_limit_five(mk):
    rest = FakeRest(xrp=1000.0)
    wd, st, clock, msgs = mk(rest=rest)
    for _ in range(5):
        wd.execute(wd.preview(5))
    with pytest.raises(W.WithdrawError, match="5회"):
        wd.preview(5)
    clock.t += 24 * 3_600_000                                           # 다음 날(KST)이면 다시
    assert wd.preview(5)["left_today"] == 5


def test_price_move_over_3pct_refuses(mk):
    http = FakeHTTP()
    rest = FakeRest()
    wd, *_ = mk(rest=rest, http=http)
    p = wd.preview(10)
    http.xrp_krw *= 0.95                                                # 업비트 XRP 5% 하락 → 보낼 수량 5% 넘게 늘어남
    with pytest.raises(W.WithdrawError, match="3%"):
        wd.execute(p)
    assert "/v5/asset/withdraw/create" not in rest.paths() and "/v5/order/create" not in rest.paths()


def test_bought_xrp_not_visible_means_no_withdraw(mk):
    rest = FakeRest(fill=False)
    wd, st, clock, msgs = mk(rest=rest)
    with pytest.raises(W.WithdrawError, match="잔고"):
        wd.execute(wd.preview(10))
    assert "/v5/asset/withdraw/create" not in rest.paths()
    assert wd.used_today() == 0 and st.get("withdraw:log")[-1]["status"] == "fail" and "출금 안 됨" in msgs[-1]


def test_network_error_on_withdraw_is_unknown_and_counts(mk):
    rest = FakeRest(xrp=100.0, wd_error=BybitNetError("/v5/asset/withdraw/create ReadTimeout"))
    wd, st, clock, msgs = mk(rest=rest)
    with pytest.raises(W.WithdrawError, match="결과 모름"):
        wd.execute(wd.preview(10))
    assert st.get("withdraw:log")[-1]["status"] == "unknown" and wd.used_today() == 1
    assert rest.paths().count("/v5/asset/withdraw/create") == 1         # 다시 보내지 않음


def test_exchange_rejection_is_fail_and_not_counted(mk):
    rest = FakeRest(xrp=100.0, wd_error=BybitError(131002, "address not in whitelist", "/v5/asset/withdraw/create"))
    wd, st, clock, msgs = mk(rest=rest)
    with pytest.raises(W.WithdrawError, match="whitelist"):
        wd.execute(wd.preview(10))
    assert st.get("withdraw:log")[-1]["status"] == "fail" and wd.used_today() == 0


def test_only_one_withdrawal_at_a_time(mk):
    wd, *_ = mk(rest=FakeRest(xrp=100.0))
    p = wd.preview(10)
    wd.lock.acquire()
    with pytest.raises(W.WithdrawError, match="진행 중"):
        wd.execute(p)
    wd.lock.release()


# ---------- 만들기 ----------
def test_make_withdrawer_conditions(monkeypatch):
    from app.bybit_rest import BybitREST
    monkeypatch.setattr(BybitREST, "sync_time", lambda self: None)
    sec = {"BYBIT_LIVE_API_KEY": "k", "BYBIT_LIVE_API_SECRET": "s"}.get

    class B:
        def __init__(self, mode, enabled):
            self.mode, self.enabled, self.name, self.last_equity, self.raw_budgets = mode, enabled, mode, None, {}
    live = [B("paper", True), B("live", True)]
    assert W.make_withdrawer({"withdraw": {**WCFG, "enabled": False}}, live, None, None, sec)[0] is None
    wd, note = W.make_withdrawer({"withdraw": {**WCFG, "address": ""}}, live, None, None, sec)
    assert wd is None and "주소" in note
    assert W.make_withdrawer({"withdraw": WCFG}, [B("paper", True), B("live", False)], None, None, sec)[0] is None
    wd, note = W.make_withdrawer({"withdraw": WCFG}, live, None, None, sec)
    assert wd and "켜짐" in note and wd.rest.allowed is WITHDRAW_ALLOWED and wd.rest.retries == 1
    assert wd.rest.withdraw_dest == {"coin": "XRP", "chain": "XRP", "address": ADDR, "tag": TAG}


# ---------- 웹 ----------
class E:
    def snapshot(self):
        return {"books": [], "regime": {}}

    def command(self, c, b):
        return "접수"


class FakeWD:
    def __init__(self):
        self.done = []

    def status(self):
        return {"enabled": True, "max_usd": 100, "left_today": 5, "count_per_day": 5, "dest": "업비트 XRP rYmT…GSMV"}

    def preview(self, usd):
        if float(usd) > 100:
            raise W.WithdrawError("금액은 0 초과 100 달러 이하")
        return {"usd": float(usd), "amount": "5.000000"}

    def execute(self, p):
        self.done.append(p)
        return "💸 출금 접수"


def login(app):
    cl = TestClient(app)
    r = cl.post("/login", data={"password": "correct-horse-1"}, follow_redirects=False)
    cl.cookies.set("at_session", r.cookies["at_session"])
    return cl


def test_web_withdraw_flow(tmp_path):
    st = Store(str(tmp_path / "w.db"))
    fw = FakeWD()
    app = create_app(E(), st, "correct-horse-1", {}, withdrawer=fw, withdraw_note="켜짐")
    assert TestClient(app).post("/api/withdraw/preview", json={"usd": 10}).status_code == 401
    cl = login(app)
    assert cl.get("/api/status").json()["withdraw"]["enabled"] is True
    assert cl.post("/api/withdraw/preview", json={"usd": 500}).status_code == 400
    n = cl.post("/api/withdraw/preview", json={"usd": 10}).json()["nonce"]
    assert cl.post("/api/action", json={"nonce": n}).status_code == 400           # 다른 버튼 경로로는 실행 안 됨
    n = cl.post("/api/withdraw/preview", json={"usd": 10}).json()["nonce"]
    assert cl.post("/api/withdraw/apply", json={"nonce": n, "password": "wrong"}).status_code == 403
    assert cl.post("/api/withdraw/apply", json={"nonce": n, "password": "correct-horse-1"}).status_code == 400   # 틀리면 확인 코드도 끝
    n = cl.post("/api/withdraw/preview", json={"usd": 10}).json()["nonce"]
    assert cl.post("/api/withdraw/apply", json={"nonce": n, "password": "correct-horse-1"}).json()["msg"] == "💸 출금 접수"
    assert cl.post("/api/withdraw/apply", json={"nonce": n, "password": "correct-horse-1"}).status_code == 400   # 재사용 불가
    assert len(fw.done) == 1 and fw.done[0]["usd"] == 10


def test_web_withdraw_off(tmp_path):
    app = create_app(E(), Store(str(tmp_path / "w.db")), "correct-horse-1", {}, withdraw_note="꺼짐 (설정)")
    cl = login(app)
    assert cl.get("/api/status").json()["withdraw"] == {"enabled": False, "reason": "꺼짐 (설정)"}
    assert cl.post("/api/withdraw/preview", json={"usd": 10}).status_code == 400
