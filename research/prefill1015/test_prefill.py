"""prefill1015 시험 (합성 봉). 실행: python -m pytest -q test_prefill.py (옆에 ../trail1014 필요)"""
import os
import sys
import csv
import json
import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run_prefill as P          # noqa: E402
import trail_sim as T            # noqa: E402

B = T.BAR_MS
FLAT = (105, 106, 104, 105)


def bars_from(rows, t0=0):
    return [(t0 + i * B, *r) for i, r in enumerate(rows)]


def test_touch_only_before_fill_bar():
    up = (105, 112.5, 104, 110)                       # 고가 112.5 ≥ 100 × 1.12
    b = bars_from([up, FLAT, (101, 102, 99, 101)] + [FLAT] * 10)
    assert P.touched_before_fill(b, 2, 0, 100) is True
    b = bars_from([FLAT, FLAT, (101, 113, 99, 101)] + [FLAT] * 10)   # 체결 봉의 고가는 안 봄
    assert P.touched_before_fill(b, 2, 0, 100) is False
    b = bars_from([up, FLAT, (101, 102, 99, 101)])
    assert P.touched_before_fill(b, 2, B, 100) is False             # 신호 전 봉은 안 봄
    b = bars_from([(105, 112.0, 104, 110), (101, 102, 99, 101)])
    assert P.touched_before_fill(b, 1, 0, 100) is True              # 정확히 익절선이면 닿음


def test_current_returns_and_h5_rule():
    rows = [(105, 113, 104, 110), (101, 102, 99, 101), (102, 106, 101, 105)] + [FLAT] * 800
    r = P.current_returns(bars_from(rows), {"t_sig": 0, "limit_px": 100, "limit_until": 96 * B})
    assert r["skip"] is True and r["fill_t"] == 2 * B
    assert r["why"]["P-H5"] == "TIME" and r["why"]["P-H3"] == "TIME"
    rows = [FLAT, (101, 102, 99, 101), (102, 106, 101, 105), (104, 104, 100.4, 101)] + [FLAT] * 800
    r = P.current_returns(bars_from(rows), {"t_sig": 0, "limit_px": 100, "limit_until": 96 * B})
    assert r["skip"] is False and r["why"]["P-H5"] == "LOCK"         # +5% 뒤 다음 봉에서 잠금 손절
    assert r["ret"]["P-H5"] == pytest.approx(T.ret_pct(100, 100.5))


def test_evaluate_semantics():
    tr = [{"ret": {"P-H3": x}, "skip": s} for x, s in [(5, False), (-10, True), (2, False), (-4, True), (1, False), (3, False)]]
    c = P.evaluate(tr, "P-H3", nmin=6, smin=2)
    assert c["n_skip"] == 2 and c["mean_skipped"] == -7 and c["mean_kept"] == pytest.approx(11 / 4)
    assert c["d_mean"] == pytest.approx(14 / 6) and c["A"]                # 건너뛴 사건 평균이 마이너스 → Δ > 0
    assert not c["B"]                                                      # Δ가 큰 3개(10, 4, 0)를 빼면 0 → 통과 아님
    c = P.evaluate(tr, "P-H3", nmin=6, smin=3)
    assert not c["D"] and not c["pass"]


def write(tmp, rows_by_sym, events):
    (tmp / "bars").mkdir()
    for s, rows in rows_by_sym.items():
        with open(tmp / "bars" / f"{s}.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["t_open", "open", "high", "low", "close"])
            w.writerows(rows)
    with open(tmp / "e.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["event_id", "symbol", "t_sig", "limit_px", "limit_until", "cats_H3"])
        w.writerows(events)


def test_cli_explore_and_judge_guard(tmp_path, monkeypatch):
    import run_trail as R
    monkeypatch.setattr(R, "in_capture", lambda now=None: False)
    R._cache.clear()
    T0 = 1_790_000_000_000 // B * B
    rows = {}
    ev = []
    for k in range(6):
        s = f"S{k}"
        pre = [(105, 113, 104, 110)] if k % 2 else [FLAT]
        seq = pre + [(101, 102, 99, 101)] + [FLAT] * 700
        rows[s] = [(T0 + i * B, *r) for i, r in enumerate(seq)]
        ev.append((f"e{k}", s, T0, 100, T0 + 24 * T.H, 1))
    write(tmp_path, rows, ev)
    base = ["--events", str(tmp_path / "e.csv"), "--bars-dir", str(tmp_path / "bars"), "--out", str(tmp_path)]
    monkeypatch.setattr(sys, "argv", ["x", "--mode", "explore", "--label", "E1", "--cutoff", str(T0 + 700 * B), *base])
    P.main()
    res = json.load(open(tmp_path / "summary_E1.json", encoding="utf-8"))
    assert res["kind"] == "탐색" and res["results"]["P-H3"]["n"] == 6 and res["results"]["P-H3"]["n_skip"] == 3
    assert res["results"]["P-H3"]["D"] is False                            # N 6 < 200
    monkeypatch.setattr(sys, "argv", ["x", "--mode", "judge", "--label", "J", "--cutoff", str(T0 + 700 * B), "--variants", "P-H3", *base])
    P.main()
    res = json.load(open(tmp_path / "summary_J.json", encoding="utf-8"))
    assert res["results"] == {} and "P-H3" in res["not_judged"]           # N < 30 → 수익률 계산 안 함
    with pytest.raises(SystemExit):
        monkeypatch.setattr(sys, "argv", ["x", "--mode", "judge", "--label", "J", "--cutoff", str(T0 + 700 * B), *base])
        P.main()                                                           # judge 는 --variants 필요
