"""안전장치: 매매 코드에 출금 없음(출금은 app/withdraw.py 전용 클라이언트만, 화이트리스트 주소로만), 허용 엔드포인트만,
live 는 키+확인 파일 없으면 꺼짐, 웹에 한도 변경 경로 없음, 키 노출 없음."""
import os
import re
import glob
import pytest
from app import config as C
from app.bybit_rest import BybitREST, ALLOWED, WITHDRAW_ALLOWED
from app.main import build_books
from app.store import Store
from conftest import Clock, FakeMarket

APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")


def test_no_withdraw_or_transfer_endpoints():
    for m, p in ALLOWED:
        assert "/asset/" not in p and "withdraw" not in p.lower() and "transfer" not in p.lower()
    for m, p in WITHDRAW_ALLOWED:                          # 출금 전용 목록에도 이체(transfer)는 없음
        assert "transfer" not in p.lower()
    for f in glob.glob(os.path.join(APP, "*.py")):
        if os.path.basename(f) in ("withdraw.py", "bybit_rest.py"):   # 출금 전용 모듈·허용 목록만 예외
            continue
        code = open(f, encoding="utf-8").read()
        assert not re.search(r"/v5/asset/|withdraw\(|/withdraw/create", code), f


def test_withdraw_client_only_to_whitelisted_address_and_spot_buy():
    dest = {"coin": "XRP", "chain": "XRP", "address": "rADDR", "tag": "123"}
    w = BybitREST("live", "k", "s", retries=1, allowed=WITHDRAW_ALLOWED, withdraw_dest=dest)
    for bad in ({**dest, "address": "rOTHER"}, {**dest, "tag": "999"}, {**dest, "chain": "ETH"}, {**dest, "coin": "USDT"},
                {k: v for k, v in dest.items() if k != "tag"}):
        with pytest.raises(PermissionError):
            w.call("POST", "/v5/asset/withdraw/create", {**bad, "amount": "10"})
    for bad in ({"category": "linear", "symbol": "XRPUSDT", "side": "Buy"}, {"category": "spot", "symbol": "XRPUSDT", "side": "Sell"},
                {"category": "spot", "symbol": "BTCUSDT", "side": "Buy"}):
        with pytest.raises(PermissionError):
            w.call("POST", "/v5/order/create", bad)
    for path in (("POST", "/v5/order/cancel"), ("POST", "/v5/position/set-leverage"), ("POST", "/v5/asset/transfer/inter-transfer")):
        with pytest.raises(PermissionError):
            w.call(*path, {})
    trade = BybitREST("live", "k", "s")                    # 매매 클라이언트는 출금 불가 (주소가 맞아도)
    with pytest.raises(PermissionError):
        trade.call("POST", "/v5/asset/withdraw/create", {**dest, "amount": "10"})
    nodest = BybitREST("live", "k", "s", allowed=WITHDRAW_ALLOWED)
    with pytest.raises(PermissionError):
        nodest.call("POST", "/v5/asset/withdraw/create", {**dest, "amount": "10"})


def test_rest_refuses_unlisted_and_demo_money_on_live():
    r = BybitREST("live", "k", "s")
    with pytest.raises(PermissionError):
        r.call("POST", "/v5/asset/withdraw/create", {})
    with pytest.raises(PermissionError):
        r.call("POST", "/v5/account/demo-apply-money", {})


def test_keys_not_in_repr():
    r = BybitREST("demo", "KEYVALUE123", "SECRETVALUE456")
    assert "KEYVALUE123" not in repr(r) and "SECRETVALUE456" not in repr(r)


def test_live_book_disabled_without_keys_and_confirm(tmp_path, monkeypatch, cfg):
    for n in ("BYBIT_LIVE_API_KEY", "BYBIT_LIVE_API_SECRET", "BYBIT_DEMO_API_KEY", "BYBIT_DEMO_API_SECRET"):
        monkeypatch.delenv(n, raising=False)
    monkeypatch.setattr(C, "LIVE_CONFIRM_FILE", str(tmp_path / "LIVE_CONFIRM.txt"))
    cfg["books"] = [{"name": "real", "mode": "live", "budgets": {"H3": 300}}, {"name": "demo", "mode": "demo", "budgets": {"H3": 300}}]
    C.validate(cfg)
    books = build_books(cfg, Store(str(tmp_path / "x.db")), FakeMarket(Clock()))
    assert not books[0].enabled and "live" in books[0].disabled_reason
    assert not books[1].enabled
    # 키가 있어도 확인 파일이 없으면 꺼짐
    monkeypatch.setenv("BYBIT_LIVE_API_KEY", "k")
    monkeypatch.setenv("BYBIT_LIVE_API_SECRET", "s")
    ok, why = C.live_allowed()
    assert not ok and "LIVE_CONFIRM" in why
    (tmp_path / "LIVE_CONFIRM.txt").write_text("yes\n", encoding="utf-8")
    assert not C.live_allowed()[0]                         # 문구가 다르면 안 됨
    (tmp_path / "LIVE_CONFIRM.txt").write_text(C.LIVE_CONFIRM_PHRASE + "\n", encoding="utf-8")
    assert C.live_allowed()[0]


def test_config_limits_cannot_exceed_user_caps(cfg):
    for path, val in ((("risk", "per_trade_risk_cap_pct"), 3.0), (("risk", "leverage_cap"), 6), (("risk", "daily_loss_limit_pct"), 7.0),
                      (("risk", "cumulative_stop_pct"), 25.0)):
        c = C._merge(cfg, {})
        c[path[0]][path[1]] = val
        with pytest.raises(C.ConfigError):
            C.validate(c)


def test_project_config_live_book_stays_off_without_keys_and_confirm(tmp_path, monkeypatch):
    # 10/13 사용자 요청으로 config.yaml 에 live 블록을 켜 둠 → 키 + LIVE_CONFIRM.txt 없으면 반드시 꺼져 있어야 함
    for n in ("BYBIT_LIVE_API_KEY", "BYBIT_LIVE_API_SECRET", "BYBIT_DEMO_API_KEY", "BYBIT_DEMO_API_SECRET"):
        monkeypatch.delenv(n, raising=False)
    monkeypatch.setattr(C, "LIVE_CONFIRM_FILE", str(tmp_path / "LIVE_CONFIRM.txt"))
    c = C.load(os.path.join(os.path.dirname(APP), "config.yaml"))
    books = build_books(c, Store(str(tmp_path / "x.db")), FakeMarket(Clock()))
    assert [b.name for b in books if b.enabled] == ["paper"]
    assert all(not b.enabled for b in books if b.mode != "paper")
    monkeypatch.setenv("BYBIT_LIVE_API_KEY", "k")
    monkeypatch.setenv("BYBIT_LIVE_API_SECRET", "s")                       # 키만 있고 확인 파일 없음 → 여전히 꺼짐
    books = build_books(c, Store(str(tmp_path / "y.db")), FakeMarket(Clock()))
    assert all(not b.enabled for b in books if b.mode == "live")


def test_web_has_no_limit_or_mode_routes(tmp_path):
    from app.web import create_app

    class E:
        def snapshot(self): return {"books": []}
        def command(self, c, b): return "ok"
    app = create_app(E(), Store(str(tmp_path / "w.db")), "x" * 12, {})
    paths = {(sorted(r.methods)[0] if getattr(r, "methods", None) else "", r.path) for r in app.routes}
    assert paths == {("GET", "/login"), ("POST", "/login"), ("GET", "/logout"), ("GET", "/"), ("GET", "/api/status"),
                     ("POST", "/api/confirm"), ("POST", "/api/action"), ("GET", "/api/export.csv"),
                     ("GET", "/api/chart"), ("GET", "/api/equity"),
                     ("POST", "/api/budget/confirm"), ("POST", "/api/budget/apply"),     # 예산만 (한도 %·레버리지·모드는 없음)
                     ("POST", "/api/withdraw/preview"), ("POST", "/api/withdraw/apply")}  # 출금 (주소는 설정 파일에만)


def test_web_requires_login_and_confirm(tmp_path):
    from fastapi.testclient import TestClient
    from app.web import create_app
    calls = []

    class E:
        def snapshot(self): return {"books": [], "regime": {}}
        def command(self, c, b):
            calls.append((c, b)); return "접수"
    st = Store(str(tmp_path / "w.db"))
    cl = TestClient(create_app(E(), st, "correct-horse-1", {}))
    assert cl.get("/api/status").status_code == 401
    assert cl.post("/api/confirm", json={"cmd": "stop"}).status_code == 401
    for _ in range(5):
        cl.post("/login", data={"password": "wrong"}, follow_redirects=False)
    r = cl.post("/login", data={"password": "correct-horse-1"}, follow_redirects=False)
    assert "at_session" not in r.cookies                     # 5번 틀리면 잠김
    cl2 = TestClient(create_app(E(), st, "correct-horse-1", {}))
    r = cl2.post("/login", data={"password": "correct-horse-1"}, follow_redirects=False)
    assert r.status_code == 303 and "at_session" in r.cookies
    cl2.cookies.set("at_session", r.cookies["at_session"])
    assert cl2.get("/api/status").status_code == 200
    assert cl2.post("/api/action", json={"nonce": "bogus"}).status_code == 400      # 확인 없이 실행 안 됨
    n = cl2.post("/api/confirm", json={"cmd": "closeall", "book": "paper"}).json()["nonce"]
    assert cl2.post("/api/action", json={"nonce": n}).json()["msg"] == "접수"
    assert cl2.post("/api/action", json={"nonce": n}).status_code == 400            # 같은 확인 코드 재사용 불가
    assert calls == [("closeall", "paper")]
    # 차트·손익 곡선: 로그인 필요, 다리 기록으로 선·표시 생성
    assert TestClient(create_app(E(), st, "correct-horse-1", {})).get("/api/chart?leg_id=1").status_code == 401
    lid = st.add_leg(book="paper", mode="paper", event_id="E", strategy="H3", leg="m", symbol="ABCUSDT", status="closed",
                     limit_px=1.0, fill_px=1.0, sl_px=0.85, fill_ts=1_792_000_000_000, exit_ts=1_792_000_600_000,
                     exit_reason="TIME", pnl_pct=1.5, pnl_usdt=0.1, created_ts=1_792_000_000_000)
    d = cl2.get(f"/api/chart?leg_id={lid}&iv=1").json()
    assert {x["title"] for x in d["lines"]} == {"지정가", "체결", "손절"} and [m["text"] for m in d["markers"]][0] == "진입"
    assert "paper H3" in cl2.get("/api/equity").json()
