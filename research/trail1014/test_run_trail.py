"""run_trail 실행 도구 시험 (합성 입력). 실행: python -m pytest -q test_run_trail.py"""
import csv
import json
import sys
import pytest
import run_trail as R
import trail_sim as T

B = T.BAR_MS
T0 = 1_790_000_000_000 // B * B
FLAT = (105, 106, 104, 105)
IN_CAPTURE = R.in_capture                                                        # 아래 autouse 패치 전 원래 함수


@pytest.fixture(autouse=True)
def no_capture(monkeypatch):
    monkeypatch.setattr(R, "in_capture", lambda now=None: False)
    R._cache.clear()


def write_events(p, rows, header=("event_id", "symbol", "t_sig", "limit_px", "limit_until", "cats_H3")):
    with open(p, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def write_bars(d, sym, n, first=(101, 102, 99, 101)):
    d.mkdir(exist_ok=True)
    with open(d / f"{sym}.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t_open", "open", "high", "low", "close"])
        w.writerow([T0, *first])
        for i in range(1, n):
            w.writerow([T0 + i * B, *FLAT])


def ev(i, sym="X", cats="1"):
    return (f"e{i}", sym, T0, 100, T0 + 24 * T.H, cats)


def run(monkeypatch, *args):
    monkeypatch.setattr(sys, "argv", ["run_trail.py", *args])
    R.main()


def test_cats_parsing_and_dedupe(tmp_path):
    p = tmp_path / "e.csv"
    write_events(p, [ev(0, "A", "1.0"), ev(1, "B", "True"), ev(2, "C", "yes"), ev(3, "D", "0"), ev(4, "A", "1")])
    evs, dup = R.load_events(p, None)
    assert [e["symbol"] for e in evs] == ["A", "B", "C"] and dup == 1
    write_events(p, [ev(0)], header=("event_id", "symbol", "t_sig", "limit_px", "limit_until", "cats.H3"))
    assert len(R.load_events(p, None)[0]) == 1
    write_events(p, [ev(0)[:5]], header=("event_id", "symbol", "t_sig", "limit_px", "limit_until"))
    with pytest.raises(SystemExit):
        R.load_events(p, None)


def test_limit_checks(tmp_path):
    p = tmp_path / "e.csv"
    write_events(p, [("e0", "X", T0, 100, T0 + 24 * T.H + B, "1")])
    with pytest.raises(SystemExit):
        R.load_events(p, None)
    write_events(p, [("e0", "X", T0, 100, T0 + 24 * T.H, "1", 106)],
                 header=("event_id", "symbol", "t_sig", "limit_px", "limit_until", "cats_H3", "c0"))
    with pytest.raises(SystemExit):
        R.load_events(p, None)


def test_bars_dedupe(tmp_path):
    d = tmp_path / "bars"
    write_bars(d, "X", 3)
    with open(d / "X.csv", "a", newline="") as f:
        csv.writer(f).writerow([T0 + B, 1, 2, 0.5, 1])
    rows, ts = R.bars_of(d, "X")
    assert len(rows) == 3 and rows[1][1] == 105


def test_after_is_strict_and_seconds_format(tmp_path):
    assert R.kst_ms("2026-10-10 13:00") == R.kst_ms("2026-10-10 13:00:00")
    p = tmp_path / "e.csv"
    write_events(p, [ev(0)])
    assert R.load_events(p, T0)[0] == [] and len(R.load_events(p, T0 - 1)[0]) == 1


def test_count_only_judge_default_nmin_30(tmp_path, monkeypatch):
    write_events(tmp_path / "e.csv", [(f"e{i}", f"S{i}", T0, 100, T0 + 24 * T.H, 1) for i in range(30)])
    for i in range(30):
        write_bars(tmp_path / "bars", f"S{i}", 700)
    run(monkeypatch, "--mode", "judge", "--label", "J", "--events", str(tmp_path / "e.csv"), "--bars-dir", str(tmp_path / "bars"),
        "--count-only", "--cutoff", str(T0 + 200 * T.H), "--out", str(tmp_path))
    out = json.load(open(next(tmp_path.glob("count_J_*.json"))))
    assert out["nmin"] == 30 and out["done_H3"] == 30 and out["H3"] == "판정 가능" and out["mode"] == "judge"


def test_judge_guards(tmp_path, monkeypatch):
    write_events(tmp_path / "e.csv", [ev(0)])
    write_bars(tmp_path / "bars", "X", 700)
    base = ["--mode", "judge", "--label", "J", "--events", str(tmp_path / "e.csv"), "--bars-dir", str(tmp_path / "bars"), "--out", str(tmp_path)]
    with pytest.raises(SystemExit):
        run(monkeypatch, *base, "--variants", "H3-L5")                          # cutoff 없음
    with pytest.raises(SystemExit):
        run(monkeypatch, *base, "--cutoff", str(T0 + 200 * T.H))                # variants 없음
    with pytest.raises(SystemExit):
        run(monkeypatch, *base, "--cutoff", str(T0 + 200 * T.H), "--variants", "H3-L3,H3-L5")   # H3 둘
    run(monkeypatch, *base, "--cutoff", str(T0 + 200 * T.H), "--variants", "H3-L5,H4-V1")
    res = json.load(open(tmp_path / "summary_J.json"))
    assert res["strategies"]["H3"]["n"] == 1 and "판정 불가" in res["strategies"]["H3"]["status"]
    assert res["variants"] == {} and res["pairs"] == {}                          # N < 30 → 수익률 계산 안 함


def test_judge_cut_stops(tmp_path, monkeypatch):
    write_events(tmp_path / "e.csv", [(f"e{i}", f"S{i}", T0, 100, T0 + 24 * T.H, 1) for i in range(30)])
    for i in range(30):
        write_bars(tmp_path / "bars", f"S{i}", 200)                             # 체결 뒤 약 50h 뿐
    with pytest.raises(SystemExit) as ex:
        run(monkeypatch, "--mode", "judge", "--label", "J", "--events", str(tmp_path / "e.csv"), "--bars-dir", str(tmp_path / "bars"),
            "--cutoff", str(T0 + 80 * T.H), "--variants", "H3-L5", "--out", str(tmp_path))
    assert "자료 끊김" in str(ex.value)
    assert not (tmp_path / "summary_J.json").exists()


def test_explore_needs_cutoff_and_runs(tmp_path, monkeypatch):
    write_events(tmp_path / "e.csv", [(f"e{i}", f"S{i}", T0, 100, T0 + 24 * T.H, 1) for i in range(5)])
    for i in range(5):
        write_bars(tmp_path / "bars", f"S{i}", 700)
    base = ["--mode", "explore", "--label", "E1", "--events", str(tmp_path / "e.csv"), "--bars-dir", str(tmp_path / "bars"), "--out", str(tmp_path)]
    with pytest.raises(SystemExit):
        run(monkeypatch, *base)
    run(monkeypatch, *base, "--cutoff", str(T0 + 700 * B))
    res = json.load(open(tmp_path / "summary_E1.json"))
    assert res["kind"] == "탐색" and res["nmin"] == 200 and len(res["pairs"]) == 5
    assert res["strategies"]["H4"]["n"] == 5 and res["pairs"]["H4-V1"]["D"] is False
    assert "탐색 불통과" in open(tmp_path / "table_E1.md", encoding="utf-8").read()


def test_capture_windows():
    import datetime as dt
    f = lambda h, m: IN_CAPTURE(dt.datetime(2026, 10, 14, h, m, tzinfo=R.KST))
    assert f(12, 55) and f(13, 34) and not f(13, 35) and not f(12, 54)
    assert f(20, 55) and not f(21, 35) and f(23, 55) and f(0, 0) and f(0, 34) and not f(0, 35)
