"""엔진 + paper 거래소 통합 시험 (가짜 1분봉): 한 사이클, 재시작 복원, 중복 차단, 일일 한도, API 실패, 정지·청산, 레짐 관문."""
import json
import pytest
from app.store import Store
from app.engine import Engine, Book
from app.exchange_paper import PaperExchange
from app.exchange_base import ExchangeError
from app.timeutil import kst_day
from conftest import Clock, FakeMarket, FakeScreener, FakeRegime, FakeNotify, make_event, T0, MIN

H = 3_600_000


def build(cfg, tmp_path, clock=None, market=None, scr=None, regime=None, notify=None, store=None):
    clock = clock or Clock()
    market = market or FakeMarket(clock)
    scr = scr or FakeScreener()
    store = store or Store(str(tmp_path / "bot.db"))
    raw = store.get("paper_state:paper")
    ex = PaperExchange(market, state=json.loads(raw) if raw else None, save=lambda s: store.put("paper_state:paper", s), clock=clock)
    book = Book(cfg["books"][0], ex, cfg["risk"])
    eng = Engine(cfg, store, market, scr, regime or FakeRegime(), notify or FakeNotify(), [book], clock=clock)
    return eng, store, ex, market, scr, clock


@pytest.fixture
def cfg2(cfg):
    cfg["books"][0]["budgets"] = {"H3": 300, "H4": 1000, "H5": 300}
    return cfg


def legs_by(store):
    return {f"{l['strategy']}{l['leg']}": l for l in store.legs("paper")}


def open_entries(eng, market, scr, clock):
    market.price["ABCUSDT"] = 1.05
    scr.events = [make_event(t=clock())]
    eng.step()
    market.add_bar("ABCUSDT", 1.05, 1.05, 1.03, 1.04)
    market.add_bar("ABCUSDT", 1.02, 1.02, 0.99, 1.0)     # 저가 0.99 < 지정가 1.0 → 체결
    eng.step()


def test_full_cycle_h3_h4_h5(cfg2, tmp_path):
    eng, store, ex, market, scr, clock = build(cfg2, tmp_path)
    market.price["ABCUSDT"] = 1.05
    scr.events = [make_event(t=clock())]
    eng.step()
    L = legs_by(store)
    assert set(L) == {"H3m", "H4a", "H4b", "H5m"}
    assert all(l["status"] == "entry_open" for l in L.values())
    assert L["H4a"]["tp_px"] == pytest.approx(1.12) and L["H4b"]["tp_px"] is None
    assert L["H3m"]["sl_px"] == pytest.approx(0.85) and L["H3m"]["lev"] == 4
    dec = store.q("SELECT strategy, decision FROM decisions")
    assert {d["strategy"]: d["decision"] for d in dec} == {"H3": "enter", "H4": "enter", "H5": "enter"}

    market.add_bar("ABCUSDT", 1.05, 1.05, 1.03, 1.04)    # 아직 지정가 위
    eng.step()
    assert all(l["status"] == "entry_open" for l in legs_by(store).values())
    market.add_bar("ABCUSDT", 1.02, 1.02, 0.99, 1.0)     # 체결
    eng.step()
    L = legs_by(store)
    assert all(l["status"] == "open" for l in L.values())
    assert all(l["fill_px"] == pytest.approx(1.0) for l in L.values())
    assert all(l["sl_order_id"] for l in L.values())
    assert L["H4a"]["tp_order_id"]
    assert any("체결" in m for m in eng.tg.msgs)

    market.add_bar("ABCUSDT", 1.0, 1.06, 1.0, 1.055)     # +6% → H5 손절 +0.5% 로
    eng.step()
    L = legs_by(store)
    assert L["H5m"]["armed"] == 1 and L["H5m"]["sl_px"] == pytest.approx(1.005)
    assert L["H3m"]["sl_px"] == pytest.approx(0.85)

    market.add_bar("ABCUSDT", 1.06, 1.13, 1.05, 1.12)    # +13% → H4a 익절
    eng.step()
    L = legs_by(store)
    assert L["H4a"]["status"] == "closed" and L["H4a"]["exit_reason"] == "TP" and L["H4a"]["pnl_pct"] > 11
    assert L["H4b"]["tp_done"] == 1 and L["H4b"]["sl_px"] == pytest.approx(1.005)

    market.add_bar("ABCUSDT", 1.12, 1.12, 1.0, 1.01)     # 1.005 깨짐 → H4b·H5 손절(+0.5%)
    eng.step()
    L = legs_by(store)
    for k in ("H4b", "H5m"):
        assert L[k]["status"] == "closed" and L[k]["exit_reason"] == "SL+", k
        assert 0 < L[k]["pnl_pct"] < 0.5, (k, L[k]["pnl_pct"])
    assert L["H3m"]["status"] == "open"

    clock.t = L["H3m"]["fill_ts"] + 72 * H + 1000           # 72시간 → 시간 청산
    eng.step(); eng.step()
    L = legs_by(store)
    assert L["H3m"]["status"] == "closed" and L["H3m"]["exit_reason"] == "TIME"
    assert L["H3m"]["hold_h"] == pytest.approx(72, abs=0.1)
    assert L["H3m"]["pnl_usdt"] == pytest.approx(L["H3m"]["exit_value"] - L["H3m"]["fill_px"] * L["H3m"]["fill_qty"]
                                                  - L["H3m"]["entry_fee"] - L["H3m"]["exit_fee"] - L["H3m"]["funding"])
    assert ex.positions() == {}
    assert not ex.open_orders()                              # 남은 보호 주문 없음
    assert sum(1 for m in eng.tg.msgs if "청산" in m) >= 3


def test_snapshot_money_breakdown(cfg2, tmp_path):
    eng, store, ex, market, scr, clock = build(cfg2, tmp_path)
    open_entries(eng, market, scr, clock)
    market.all_tickers = lambda: {"ABCUSDT": 1.02}
    s = eng.snapshot()["books"][0]
    h3 = s["strategies"]["H3"]
    assert h3["rank"] == "1위" and h3["n_open"] == 1
    l = legs_by(store)["H3m"]
    assert h3["notional"] == pytest.approx(l["fill_qty"] * l["fill_px"])
    assert h3["margin"] == pytest.approx(l["fill_qty"] * l["fill_px"] / l["lev"])
    assert h3["upnl"] == pytest.approx(l["fill_qty"] * (1.02 - l["fill_px"]))
    t = s["total"]
    assert t["margin"] == pytest.approx(sum(v["margin"] for v in s["strategies"].values()))
    assert t["n_open"] == 3
    # 청산 뒤: 이익·손실 나눔
    market.add_bar("ABCUSDT", 1.06, 1.13, 1.05, 1.12)      # H4a 익절
    eng.step()
    market.add_bar("ABCUSDT", 1.0, 1.0, 0.80, 0.82)        # 나머지 손절
    eng.step()
    s = eng.snapshot()["books"][0]
    pn = [x["pnl_usdt"] for x in store.legs("paper") if x["status"] == "closed"]
    t = s["total"]
    assert t["profit"] == pytest.approx(sum(p for p in pn if p > 0), abs=1e-3)
    assert t["loss"] == pytest.approx(sum(p for p in pn if p < 0), abs=1e-3)
    assert t["net"] == pytest.approx(sum(pn), abs=1e-3) and t["n_closed"] == len(pn)
    txt = eng.status_text()
    assert "1위 H3" in txt and "총 이익" in txt and "총 손실" in txt


def test_restart_restores_state_and_no_duplicate(cfg2, tmp_path):
    eng, store, ex, market, scr, clock = build(cfg2, tmp_path)
    open_entries(eng, market, scr, clock)
    n_legs, n_orders = len(store.legs("paper")), len(ex.orders)
    assert all(l["status"] == "open" for l in store.legs("paper"))
    # 재시작: 새 거래소 객체(저장 상태에서) + 새 엔진, 같은 DB
    eng2, store2, ex2, *_ = build(cfg2, tmp_path, clock=clock, market=market, scr=scr, store=store)
    assert len(ex2.positions()) == 1 and len(ex2.open_orders()) == len(ex.open_orders())
    eng2.step(); eng2.step()
    assert len(store.legs("paper")) == n_legs                  # 같은 신호로 새 주문 없음
    assert len(ex2.orders) == n_orders
    assert all(l["status"] == "open" for l in store.legs("paper"))


def test_fill_while_bot_down_is_picked_up(cfg2, tmp_path):
    eng, store, ex, market, scr, clock = build(cfg2, tmp_path)
    market.price["ABCUSDT"] = 1.05
    scr.events = [make_event(t=clock())]
    eng.step()
    # 봇 꺼짐 → 그동안 체결·손절까지 일어남
    market.add_bar("ABCUSDT", 1.02, 1.02, 0.99, 1.0)
    market.add_bar("ABCUSDT", 1.0, 1.0, 0.80, 0.82)       # −20% → 손절 −15% 체결
    eng2, *_ = build(cfg2, tmp_path, clock=clock, market=market, scr=scr, store=store)
    eng2.step(); eng2.step()
    L = legs_by(store)
    assert all(l["status"] == "closed" and l["exit_reason"] == "SL" for l in L.values())
    assert all(-16.5 < l["pnl_pct"] < -14.9 for l in L.values())


def test_duplicate_order_blocked(cfg2, tmp_path):
    eng, store, ex, market, scr, clock = build(cfg2, tmp_path)
    market.price["ABCUSDT"] = 1.05
    ev = make_event(t=clock())
    scr.events = [ev]
    eng.step()
    n = len(store.legs("paper"))
    eng.handle_entry(eng.books[0], ev, "H3")                   # 같은 사건을 억지로 한 번 더
    eng.step()
    assert len(store.legs("paper")) == n
    l = store.legs("paper")[0]
    with pytest.raises(ExchangeError):
        ex.place_entry("ABCUSDT", 5, 1.0, l["entry_link"], 0.85)   # 같은 orderLinkId 는 거래소가 거부


def test_daily_loss_limit_blocks_new_entries(cfg2, tmp_path):
    eng, store, ex, market, scr, clock = build(cfg2, tmp_path)
    store.add_leg(book="paper", mode="paper", event_id="OLD", strategy="H3", leg="m", symbol="OLDUSDT", status="closed",
                  pnl_usdt=-18.5, exit_ts=clock() - 1000, fill_px=1, fill_qty=1)
    assert kst_day(clock() - 1000) == kst_day(clock())
    market.price["ABCUSDT"] = 1.05
    scr.events = [make_event(t=clock())]
    eng.step()
    d = {x["strategy"]: x for x in store.q("SELECT * FROM decisions WHERE event_id!='OLD'")}
    assert d["H3"]["decision"] == "skip" and "일일 손실" in d["H3"]["reason"]
    assert d["H5"]["decision"] == "enter"
    assert any("일일 손실 한도" in m for m in eng.tg.msgs)


def test_api_failure_blocks_new_entries_until_resume(cfg2, tmp_path):
    eng, store, ex, market, scr, clock = build(cfg2, tmp_path)
    market.price["ABCUSDT"] = 1.05
    market.price["XYZUSDT"] = 10.5

    def boom(*a, **k):
        raise ExchangeError("network down")
    ex.place_entry = boom
    scr.events = [make_event(t=clock())]
    eng.step()
    assert store.get("paper:blocked")
    assert any(l["status"] == "rejected" for l in store.legs("paper"))
    assert any("신규 진입 중단" in m for m in eng.tg.msgs)
    clock.adv(MIN)
    scr.events.append(make_event("XYZUSDT", t=clock(), c0=10.526, limit=10.0))
    eng.step()
    d = store.q("SELECT * FROM decisions WHERE event_id LIKE 'XYZ%'")
    assert d and all(x["decision"] == "skip" and "진입 막힘" in x["reason"] for x in d)
    eng.command("resume", "paper")
    eng.step()
    assert store.get("paper:blocked") is None


def test_stop_cancels_pending_and_closeall_flattens(cfg2, tmp_path):
    eng, store, ex, market, scr, clock = build(cfg2, tmp_path)
    market.price["ABCUSDT"] = 1.05
    scr.events = [make_event(t=clock())]
    eng.step()
    assert eng.command("stop", "paper").startswith("접수")
    eng.step()
    assert all(l["status"] == "cancelled" for l in store.legs("paper"))
    assert store.get("paper:halt") == "수동 정지"
    eng.command("resume", "paper"); eng.step()
    assert store.get("paper:halt") is None
    # 새 사건 → 체결 → 전체 청산
    clock.adv(MIN)
    scr.events = [make_event("XYZUSDT", t=clock(), c0=10.526, limit=10.0)]
    market.price["XYZUSDT"] = 10.5
    eng.step()
    market.add_bar("XYZUSDT", 10.2, 10.2, 9.9, 10.0)
    eng.step()
    assert all(l["status"] == "open" for l in store.legs("paper") if l["symbol"] == "XYZUSDT")
    eng.command("closeall", "paper"); eng.step(); eng.step()
    assert all(l["status"] == "closed" and l["exit_reason"] == "CLOSEALL" for l in store.legs("paper") if l["symbol"] == "XYZUSDT")
    assert ex.positions() == {}


def test_regime_gate_and_late_signals(cfg2, tmp_path):
    cfg2["books"][0]["regime_gate"] = {"k1_down": "block", "low_vol": "block"}
    eng, store, ex, market, scr, clock = build(cfg2, tmp_path, regime=FakeRegime(k1=-1, k7=0))
    market.price["ABCUSDT"] = 1.05
    market.price["XYZUSDT"] = 10.5
    scr.events = [make_event(t=clock()),
                  make_event("XYZUSDT", t=clock() - 25 * H, c0=10.5, limit=10.0)]   # 지정가 24h 지남
    eng.step()
    d = {(x["event_id"][:3], x["strategy"]): x["reason"] for x in store.q("SELECT * FROM decisions")}
    assert "K1 하락" in d[("ABC", "H3")]
    assert "유효 시간" in d[("XYZ", "H3")]
    assert not store.legs("paper")
    eng2, store2, *_ = build(cfg2, tmp_path / "b", regime=FakeRegime(k1=None, k7=None))
    eng2.m.price["ABCUSDT"] = 1.05
    eng2.scr.events = [make_event(t=eng2.clock())]
    eng2.step()
    assert all("계산 안 됨" in x["reason"] for x in store2.q("SELECT * FROM decisions"))


def test_budget_below_minimum_disables_only_that_strategy(cfg2, tmp_path):
    cfg2["books"][0]["budgets"] = {"H3": 100, "H4": 350, "H5": 180}
    eng, store, ex, market, scr, clock = build(cfg2, tmp_path)
    b = eng.books[0]
    assert b.budgets["H3"] == 0 and "최소 180" in b.notes["H3"]
    assert b.budgets["H4"] == 350 and b.budgets["H5"] == 180
    snap = eng.snapshot()["books"][0]["strategies"]
    assert snap["H3"]["note"] and not snap["H4"]["note"]
    assert "최소" in eng.status_text()
    market.price["ABCUSDT"] = 1.05
    scr.events = [make_event(t=clock())]
    eng.step()
    strats = {l["strategy"] for l in store.legs("paper")}
    assert strats == {"H4", "H5"}                                # H3 는 꺼져서 주문 없음
    # 최소 금액에서 크기: 손절 위험 ≤ 0.5%
    for l in store.legs("paper"):
        assert l["risk_usdt"] <= b.budgets[l["strategy"]] * 0.005 + 1e-9


def test_budget_change_in_config_file_applies_without_restart(cfg2, tmp_path):
    import os
    import yaml
    path = tmp_path / "config.yaml"
    raw = {"books": [{"name": "paper", "mode": "paper", "budgets": {"H3": 300, "H4": 1000, "H5": 300},
                      "regime_gate": {"k1_down": "record", "low_vol": "record"}}]}
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    eng, store, ex, market, scr, clock = build(cfg2, tmp_path)
    eng.cfg_path, eng.cfg_mtime = str(path), eng._mtime()
    eng.cfg_mtime = os.path.getmtime(path)
    raw["books"][0]["budgets"] = {"H3": 500, "H4": 200, "H5": 0}
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    os.utime(path, (eng.cfg_mtime + 5, eng.cfg_mtime + 5))
    clock.adv(2 * MIN)
    eng.step()
    b = eng.books[0]
    assert b.budgets == {"H3": 500.0, "H4": 0.0, "H5": 0.0} and "최소 350" in b.notes["H4"]
    assert b.risk.budgets["H3"] == 500
    assert any("예산 변경" in m or "예산 H3" in m for m in eng.tg.msgs) or any("H3 300→500" in m for m in eng.tg.msgs)
    # 잘못된 설정 → 이전 값 유지
    path.write_text("risk: {leverage_cap: 99}\n" + yaml.safe_dump(raw), encoding="utf-8")
    os.utime(path, (eng.cfg_mtime + 10, eng.cfg_mtime + 10))
    clock.adv(2 * MIN)
    eng.step()
    assert b.budgets["H3"] == 500 and any("이전 설정 유지" in m for m in eng.tg.msgs)


def test_pending_orders_cancelled_when_budget_shrinks(cfg2, tmp_path):
    cfg2["books"][0]["budgets"] = {"H3": 3000, "H4": 3000, "H5": 3000}
    eng, store, ex, market, scr, clock = build(cfg2, tmp_path)
    market.price["ABCUSDT"] = 1.05
    scr.events = [make_event(t=clock())]
    eng.step()
    assert all(l["status"] == "entry_open" for l in store.legs("paper"))
    # 예산을 줄임 (H3 180: 3000 기준 위험 7.5 USDT = 4.2% > 2%), H5 는 끔
    eng.books[0].set_budgets({"H3": 180, "H4": 3000, "H5": 0})
    eng.step(); eng.step()
    L = legs_by(store)
    assert L["H3m"]["status"] == "cancelled" and "상한" in L["H3m"]["reason"]
    assert L["H5m"]["status"] == "cancelled" and "꺼짐" in L["H5m"]["reason"]
    assert L["H4a"]["status"] == "entry_open" and L["H4b"]["status"] == "entry_open"
    assert any("대기 주문 취소" in m for m in eng.tg.msgs)
    assert L["H4a"]["tp_px"] == round(L["H4a"]["tp_px"], 4)       # 익절가 소수점 꼬리 없음


def test_regime_change_notifies_and_gate_state(cfg2, tmp_path):
    cfg2["books"][0]["regime_gate"] = {"k1_down": "block", "low_vol": "block"}
    reg = FakeRegime(k1=1, k7=-1)
    eng, store, ex, market, scr, clock = build(cfg2, tmp_path, regime=reg)
    eng.step()
    b = eng.books[0]
    assert eng.gate_state(b) == "막힘 (저변동)"
    assert not any("레짐 바뀜" in m for m in eng.tg.msgs)            # 처음엔 알림 없음
    reg.k7 = 0
    eng.step()
    msg = [m for m in eng.tg.msgs if "레짐 바뀜" in m]
    assert msg and "저변동→중변동" in msg[0] and "막힘 (저변동) → 열림" in msg[0]
    assert eng.gate_state(b) == "열림"
    reg.k1 = -1
    eng.step()
    assert "하락" in [m for m in eng.tg.msgs if "레짐 바뀜" in m][-1]
    assert eng.snapshot()["books"][0]["gate_state"] == "막힘 (하락장)"
    assert "지금 진입: 막힘 (하락장)" in eng.status_text()


def test_regime_explain_text():
    from app.regime import explain
    lines = explain({"k1": 1, "k7": -1, "close": 83299, "sma50": 80994, "sma50_10d_ago": 78116, "rv30": 32.3, "rv_pct": 29,
                     "rv_q33": 33.6, "rv_q67": 43.1})
    assert "50일 평균 80,994 위" in lines[0] and "오르는 중" in lines[0]
    assert "저변동" in lines[1] and "33.6" in lines[1]


def test_site_already_filled_is_skipped(cfg2, tmp_path):
    eng, store, ex, market, scr, clock = build(cfg2, tmp_path)
    market.price["ABCUSDT"] = 1.05
    scr.events = [make_event(t=clock(), status="filled")]
    eng.step()
    assert all(x["decision"] == "skip" and "이미 filled" in x["reason"] for x in store.q("SELECT * FROM decisions"))


def test_missing_stop_is_recreated(cfg2, tmp_path):
    eng, store, ex, market, scr, clock = build(cfg2, tmp_path)
    open_entries(eng, market, scr, clock)
    l = legs_by(store)["H3m"]
    ex.cancel("ABCUSDT", order_id=l["sl_order_id"])            # 손절 주문이 사라진 상황
    clock.adv(2 * MIN)
    eng.step()
    l2 = legs_by(store)["H3m"]
    assert l2["sl_order_id"] and l2["sl_order_id"] != l["sl_order_id"]
    assert any(o.order_id == l2["sl_order_id"] and o.status == "Untriggered" for o in ex.orders.values())
    assert store.q("SELECT * FROM events WHERE kind='손절 재설정'")


def test_site_result_paired(cfg2, tmp_path):
    eng, store, ex, market, scr, clock = build(cfg2, tmp_path)
    open_entries(eng, market, scr, clock)
    scr.events[0]["lim"] = {"status": "filled", "final": {"H72": 1.23, "PT12_168": 4.5, "LK5_168": 0.39}}
    clock.t = legs_by(store)["H3m"]["fill_ts"] + 72 * H + 1000
    market.price["ABCUSDT"] = 1.01
    eng.last_site = 0
    eng.step(); eng.step()
    eng.last_site = 0
    eng.step()
    assert legs_by(store)["H3m"]["site_result"] == pytest.approx(1.23)
