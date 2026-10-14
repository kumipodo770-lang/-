"""trail_sim 규칙 시험 (합성 봉). 실행: python -m pytest -q test_trail_sim.py"""
import pytest
import trail_sim as T

B = T.BAR_MS
E = 100.0                      # 진입가(=지정가)


def bars_from(rows, t0=0):
    """rows: [(o,h,l,c)] → 15분 간격 봉"""
    return [(t0 + i * B, *r) for i, r in enumerate(rows)]


FLAT = (105, 106, 104, 105)    # 아무 일도 없는 봉 (손절·익절·트리거 밖, 지정가 위)


def fill_then(rows):
    """0번 봉에서 체결(저가 99 < 100, 손절 85 위), 그 뒤 rows"""
    return bars_from([(101, 102, 99, 101)] + rows)


def test_fill_rule():
    b = bars_from([(101, 102, 100, 101), (101, 102, 99.9, 101)])
    assert T.find_fill(b, 0, 100, 10 * B) == 1          # 저가 = 지정가 → 체결 아님, 더 낮아야
    assert T.find_fill(b, B, 100, 10 * B) == 1          # t_sig 이전 봉은 안 봄
    assert T.find_fill(b, 0, 100, B) is None             # 유효 시간 지나면 없음
    assert T.find_fill(bars_from([(101, 102, 99, 101)]), B, 100, 10 * B) is None


def test_fill_bar_stop_is_adverse():
    b = bars_from([(101, 120, 80, 90)] + [FLAT] * 400)   # 체결 봉 안에 −15%와 +12%·트리거 모두
    x, why, j = T.run_leg(b, 0, E, 72, tp_pct=12)
    assert (x, why, j) == (85.0, "SL", 0)


def test_fill_bar_ignores_tp_and_trigger():
    b = bars_from([(101, 120, 99, 101)] + [FLAT] * 400)  # 체결 봉 고가 +20%
    assert T.run_leg(b, 0, E, 72, tp_pct=12)[1] == "TIME"
    x, why, _ = T.run_leg(b, 0, E, 72, trig_pct=3)
    assert why == "TIME" and x == 105


def test_same_bar_stop_beats_tp():
    b = fill_then([(100, 115, 84, 90)] + [FLAT] * 400)
    assert T.run_leg(b, 0, E, 168, tp_pct=12)[:2] == (85.0, "SL")


def test_stop_gap_uses_open():
    b = fill_then([(80, 81, 70, 75)] + [FLAT] * 400)
    assert T.run_leg(b, 0, E, 72)[:2] == (80, "SL")


def test_tp_gap_uses_open():
    b = fill_then([(115, 116, 113, 114)] + [FLAT] * 800)
    assert T.run_leg(b, 0, E, 168, tp_pct=12)[:2] == (115, "TP")
    b = fill_then([(105, 113, 104, 110)] + [FLAT] * 800)
    assert T.run_leg(b, 0, E, 168, tp_pct=12)[:2] == (112.00000000000001, "TP")


def test_trigger_arms_next_bar_only():
    # 1번 봉: 고가 +5% 트리거, 저가 100(잠금 100.5 아래) → 같은 봉에서는 잠금 청산 안 함
    b = fill_then([(102, 105, 100, 103), (103, 104, 100.2, 101)] + [FLAT] * 400)
    x, why, j = T.run_leg(b, 0, E, 72, trig_pct=5)
    assert why == "LOCK" and j == 2 and x == pytest.approx(100.5)


def test_lock_gap_uses_open():
    b = fill_then([(102, 108, 101, 101), (99, 100, 98, 99)] + [FLAT] * 400)
    assert T.run_leg(b, 0, E, 72, trig_pct=5)[:2] == (99, "LOCK")


def test_trigger_bar_stop_wins():
    b = fill_then([(100, 110, 84, 90)] + [FLAT] * 400)
    assert T.run_leg(b, 0, E, 72, trig_pct=5)[1] == "SL"


def test_time_exit_at_open_of_due_bar():
    # 체결 시각 = 0번 봉 마감(B) → due = B + 72h = 289번 봉 시작. 288번 봉까지는 손절을 본다.
    b = fill_then([FLAT] * 287 + [(90, 91, 84, 88)] + [(107, 108, 106, 107)] + [FLAT] * 10)
    assert T.run_leg(b, 0, E, 72)[:3] == (85.0, "SL", 288)
    b = fill_then([FLAT] * 287 + [(106, 107, 105, 106)] + [(107, 108, 106, 107)] + [FLAT] * 10)
    assert T.run_leg(b, 0, E, 72)[:3] == (107, "TIME", 289)


def test_time_exit_exact():
    b = fill_then([FLAT] * 300)
    x, why, j = T.run_leg(b, 0, E, 72)
    assert why == "TIME" and b[j][0] == B + 72 * T.H and x == 105


def test_cut_when_data_ends():
    b = fill_then([FLAT] * 10)
    assert T.run_leg(b, 0, E, 72)[:2] == (105, "CUT")


def test_h4_current_b_locks_after_a_tp():
    rows = [(105, 113, 104, 110),                         # 1번 봉: a 익절 (+12%)
            (110, 111, 100.3, 101)] + [FLAT] * 800       # 2번 봉: b 잠금 100.5 닿음
    r = T.simulate_event(fill_then(rows), 0, 100, 100 * B)
    assert r["why"]["H4-현행"] == "TP+LOCK"
    want = 0.5 * T.ret_pct(E, 112.00000000000001) + 0.5 * T.ret_pct(E, 100.5)
    assert r["ret"]["H4-현행"] == pytest.approx(want)
    assert r["why"]["H4-V2"] == "TP+TIME"                 # 이동 없음 → 끝까지
    assert r["why"]["H4-V1"] == "LOCK"                    # 반익절 없이 전량 잠금
    assert r["ret"]["H4-V1"] == pytest.approx(T.ret_pct(E, 100.5))


def test_h4_b_not_locked_in_tp_bar():
    rows = [(105, 113, 100.2, 110)] + [FLAT] * 800        # 익절 봉 저가가 잠금가 아래 → b는 그 봉에선 그대로
    r = T.simulate_event(fill_then(rows), 0, 100, 100 * B)
    assert r["why"]["H4-현행"] == "TP+TIME"


def test_fees():
    assert T.ret_pct(100, 100) == pytest.approx(-0.02 - 0.055)
    assert T.ret_pct(100, 85) == pytest.approx(-15 - 0.02 - 0.85 * 0.055)


def test_stats_and_compare():
    s = T.stats([1, 2, 3, 4, 10])
    assert s["n"] == 5 and s["win"] == 100 and s["mean"] == 4
    assert s["mean_ex_top3"] == 1.5                       # 10,4,3 제외
    assert s["mean_first"] == 1.5 and s["mean_second"] == pytest.approx(17 / 3)   # 홀수 → 가운데는 뒤 절반
    c = T.compare([1, 1, 1, 1, 5, 1], [0, 0, 0, 0, 0, 0], n_min=6)
    assert c["A"] and c["B"] and c["C"] and c["D"] and c["pass"]
    c = T.compare([0, 0, 0, 9, 9, 9, 0], [0.1] * 7, n_min=5)
    assert c["A"] and not c["B"] and not c["pass"]        # 상위 3개가 전부
    c = T.compare([1, 1, -2, -2], [0, 0, 0, 0], n_min=1)
    assert not c["A"] and not c["C"]


# ---------- 경계·변경 기록 (2026-10-14 16:14) ----------
def test_fill_window_end():
    rows = [FLAT] * 95 + [(101, 102, 99, 101), (101, 102, 99, 101)]
    b = bars_from(rows)
    assert T.find_fill(b, 0, 100, 96 * B) == 95            # 마지막 봉(24h 창 안)은 체결
    b2 = bars_from([FLAT] * 96 + [(101, 102, 99, 101)])
    assert T.find_fill(b2, 0, 100, 96 * B) is None          # limit_until 에 시작하는 봉은 안 됨


def test_exact_levels_count_as_touch():
    b = bars_from([(101, 102, 85.0, 101)] + [FLAT] * 400)
    assert T.run_leg(b, 0, E, 72)[:2] == (85.0, "SL")       # 체결 봉 저가 = 손절가
    b = fill_then([(100, 101, 85.0, 90)] + [FLAT] * 400)
    assert T.run_leg(b, 0, E, 72)[:2] == (85.0, "SL")       # 다음 봉 저가 = 손절가
    b = fill_then([(105, 112.0, 104, 110)] + [FLAT] * 800)
    assert T.run_leg(b, 0, E, 168, tp_pct=12)[1] == "TP"    # 고가 = 정확히 +12%
    b = fill_then([(102, 105.0, 101, 103), (103, 104, 100.5, 101)] + [FLAT] * 400)
    assert T.run_leg(b, 0, E, 72, trig_pct=5)[:2] == (pytest.approx(100.5), "LOCK")


def test_end_of_data_time_vs_cut():
    b = fill_then([FLAT] * 288)                              # 마지막 봉 마감 = due → 시간 청산(마지막 종가)
    assert T.run_leg(b, 0, E, 72)[:2] == (105, "TIME")
    b = fill_then([FLAT] * 9 + [(106, 107, 104, 104.5)])     # due 전에 끝 → 자료 끊김, 마지막 종가
    assert T.run_leg(b, 0, E, 72)[:2] == (104.5, "CUT")


def test_missing_due_bar_uses_next_open():
    b = fill_then([FLAT] * 287)
    b = b + [(b[-1][0] + 3 * B, 103, 104, 102, 103)]        # due 봉(289번)과 그다음이 빠짐
    assert T.run_leg(b, 0, E, 72)[:2] == (103, "TIME")


def test_x_grid_and_holds():
    rows = [(101, 104, 100.8, 103), (103, 103.5, 100.4, 101)] + [FLAT] * 800   # +4% 뒤 100.4
    r = T.simulate_event(fill_then(rows), 0, 100, 100 * B)
    assert r["why"]["H3-L3"] == "LOCK" and r["why"]["H3-L5"] == "TIME" and r["why"]["H3-L8"] == "TIME"
    assert r["why"]["H4-V1"] == "TIME"                        # V1 트리거는 +12%
    assert r["fill_t"] == B
    b = fill_then([FLAT] * 800)
    assert T.run_leg(b, 0, E, 72)[2] == 289 and T.run_leg(b, 0, E, 168)[2] == 673


def test_h4_v2_weights_and_hold():
    rows = [(105, 113, 104, 110)] + [FLAT] * 800
    r = T.simulate_event(fill_then(rows), 0, 100, 100 * B)
    want = 0.5 * T.ret_pct(E, 112.00000000000001) + 0.5 * T.ret_pct(E, 105)
    assert r["ret"]["H4-V2"] == pytest.approx(want)


def test_names_subset():
    r = T.simulate_event(fill_then([FLAT] * 800), 0, 100, 100 * B, names=["H4-V1", "H4-현행"])
    assert set(r["ret"]) == {"H4-V1", "H4-현행"}


def test_window_done_boundary():
    assert T.window_done(0, "H3", 72 * T.H) and not T.window_done(1, "H3", 72 * T.H)
    assert T.window_done(0, "H4", 168 * T.H) and not T.window_done(0, "H4", 168 * T.H - 1)


def test_win_is_strictly_positive_and_z():
    s = T.stats([0, 0, 1, -1])
    assert s["win"] == 25
    c = T.compare([1, 2, 3], [0, 0, 0], 1)
    assert c["z"] == pytest.approx(2 / (1 / 3 ** 0.5))


def test_sort_key_ties():
    ts = [{"fill_t": 5, "t_sig": 2, "symbol": "B", "event_id": "x"}, {"fill_t": 5, "t_sig": 1, "symbol": "Z", "event_id": "y"},
          {"fill_t": 5, "t_sig": 1, "symbol": "A", "event_id": "z"}]
    assert [t["event_id"] for t in sorted(ts, key=T.sort_key)] == ["z", "y", "x"]
