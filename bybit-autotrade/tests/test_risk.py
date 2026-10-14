"""리스크 모듈 단독 시험: 한도 초과 거부, 일일 손실 차단, 청산가 안쪽 거부, 중복 차단, 누적 정지, 감소 주문."""
import pytest
from app.risk import RiskManager, EntryIntent, LegPlan, RiskState

INST = {"status": "Trading", "tick": 0.0001, "min_qty": 1.0, "qty_step": 1.0, "min_notional": 5.0, "max_lev": 25.0}


@pytest.fixture
def rm(cfg):
    return RiskManager(cfg["risk"], {"H3": 300, "H4": 300, "H5": 300}, "paper")


def intent(qty=5, price=1.0, sl=0.85, strategy="H3", eid="E1", symbol="ABCUSDT", lev=None, legs=None):
    return EntryIntent("paper", "paper", strategy, eid, symbol, price, sl, legs or [LegPlan("m", qty)], lev)


def test_size_uses_target_risk_and_min_notional(rm):
    q, why = rm.size("H3", 1.0, 0.85, INST)
    assert why is None
    assert q == 5                                   # 목표 0.25% → 4.97개 → 최소 주문 5 USDT 로 올림
    risk_pct = rm.loss_at_sl(q, 1.0, 0.85) / 300 * 100
    assert 0.25 <= risk_pct <= 0.5


def test_min_notional_forcing_too_much_risk_is_refused(cfg):
    small = RiskManager(cfg["risk"], {"H3": 50}, "paper")
    q, why = small.size("H3", 1.0, 0.85, INST)
    assert q == 0 and "최소 주문" in why            # 50 USDT 예산에서 5 USDT 포지션 = 위험 1.5% > 0.5%


def test_ok_entry_and_auto_leverage(rm):
    d = rm.check_entry(intent(), RiskState(), INST, 0.01)
    assert d.ok, d.reason
    assert d.leverage == 4                          # 손절 15% + 여유 5%p + 유지증거금 1% → 4배
    assert d.risk_pct <= 2.0


def test_reject_over_risk_cap(rm):
    d = rm.check_entry(intent(qty=60), RiskState(), INST, 0.01)    # 60개 × 0.15 = 9 USDT = 3% > 2%
    assert not d.ok and "상한" in d.reason


def test_reject_when_liquidation_inside_stop(rm):
    d = rm.check_entry(intent(lev=5), RiskState(), INST, 0.01)     # 5배: 청산 ≈ −19% < 손절 15% + 여유 5%p
    assert not d.ok and "청산가" in d.reason
    assert not rm.liq_ok(1.0, 0.85, 5, 0.01)
    assert rm.liq_ok(1.0, 0.85, 4, 0.01)
    assert rm.choose_leverage(1.0, 0.85, 0.01, 25) == 4
    assert rm.choose_leverage(1.0, 0.40, 0.01, 25) is None or rm.choose_leverage(1.0, 0.40, 0.01, 25) >= 1


def test_leverage_never_above_cap(cfg):
    r = dict(cfg["risk"])
    r["liq_buffer_pct"] = 0.1
    rm = RiskManager(r, {"H3": 300}, "paper")
    assert rm.choose_leverage(1.0, 0.99, 0.0, 100) == 5           # 손절이 아주 가까워도 상한 5배


def test_daily_loss_limit_blocks_new_entries(rm):
    st = RiskState(realized_today={"H3": -18.0})                   # 300 × 6% = 18
    d = rm.check_entry(intent(), st, INST, 0.01)
    assert not d.ok and "일일 손실" in d.reason
    assert rm.daily_hit(st) == ["H3"]
    d2 = rm.check_entry(intent(), RiskState(realized_today={"H3": -17.9}), INST, 0.01)
    assert d2.ok


def test_daily_limit_is_per_strategy_only(rm):
    # H3 가 하루에 크게 잃어도(−40 = 13%) 한도 안인 H4(−10)·H5(−10)는 계속 진입 (방법마다 독립, 합계 기준 없음)
    st = RiskState(realized_today={"H3": -40.0, "H4": -10.0, "H5": -10.0})
    assert not rm.check_entry(intent(strategy="H3"), st, INST, 0.01).ok
    assert rm.check_entry(intent(strategy="H4"), st, INST, 0.01).ok
    assert rm.check_entry(intent(strategy="H5"), st, INST, 0.01).ok
    assert rm.daily_hit(st) == ["H3"]


def test_cumulative_stop_halts_strategy(rm):
    st = RiskState(realized_cum={"H3": -60.0})                     # 300 × 20%
    d = rm.check_entry(intent(), st, INST, 0.01)
    assert not d.ok and "누적" in d.reason
    assert "H3" in rm.new_halts(st)


def test_duplicate_and_same_symbol_and_foreign_blocked(rm):
    assert not rm.check_entry(intent(), RiskState(decided={("E1", "H3")}), INST, 0.01).ok
    st = RiskState(open_events={"H3": {"E0": {"symbol": "ABCUSDT", "margin": 1.0}}})
    d = rm.check_entry(intent(eid="E2"), st, INST, 0.01)
    assert not d.ok and "같은 코인" in d.reason
    st2 = RiskState(open_events={"H3": {"E1": {"symbol": "ZZZUSDT", "margin": 1.0}}})
    assert not rm.check_entry(intent(eid="E1"), st2, INST, 0.01).ok
    d3 = rm.check_entry(intent(), RiskState(foreign_symbols={"ABCUSDT"}), INST, 0.01)
    assert not d3.ok and "봇 밖" in d3.reason


def test_halts_block_entries(rm):
    for st in (RiskState(halted_global="수동 정지"), RiskState(entries_blocked="API"), RiskState(halted_strategy={"H3": "누적"})):
        assert not rm.check_entry(intent(), st, INST, 0.01).ok


def test_concurrency_and_margin_limits(cfg):
    r = dict(cfg["risk"]); r["max_positions_per_strategy"] = 2
    rm = RiskManager(r, {"H3": 300}, "paper")
    st = RiskState(open_events={"H3": {"a": {"symbol": "A", "margin": 1}, "b": {"symbol": "B", "margin": 1}}})
    assert "동시" in rm.check_entry(intent(), st, INST, 0.01).reason
    rm2 = RiskManager(cfg["risk"], {"H3": 300}, "paper")
    st2 = RiskState(open_events={"H3": {"a": {"symbol": "A", "margin": 299.5}}})
    assert "증거금" in rm2.check_entry(intent(), st2, INST, 0.01).reason


def test_requires_stop_loss_and_valid_tp(rm):
    assert "손절" in rm.check_entry(intent(sl=0), RiskState(), INST, 0.01).reason
    assert "손절" in rm.check_entry(intent(sl=1.1), RiskState(), INST, 0.01).reason
    bad_tp = intent(legs=[LegPlan("a", 5, 0.9)])
    assert "익절" in rm.check_entry(bad_tp, RiskState(), INST, 0.01).reason


def test_min_order_and_step(rm):
    assert "최소" in rm.check_entry(intent(qty=4), RiskState(), INST, 0.01).reason
    inst = dict(INST, qty_step=1.0)
    assert "단위" in rm.check_entry(intent(legs=[LegPlan("m", 5.5)]), RiskState(), inst, 0.01).reason


def test_mode_mismatch(rm):
    it = intent()
    it.mode = "live"
    assert not rm.check_entry(it, RiskState(), INST, 0.01).ok


def test_reduce_only_allowed_even_when_halted(rm):
    assert rm.check_reduce(5, 5).ok
    assert not rm.check_reduce(6, 5).ok
    assert not rm.check_reduce(0, 5).ok
