"""
trail1014 실행 (PC에서). 결과는 이 폴더(= backend\\out in\\trail1014\\)에 저장.

입력
- --events CSV: event_id,symbol,t_sig,limit_px,limit_until[,cats_H3]  (시각은 UTC ms, t_sig = 신호 15분봉 마감)
  cats_H3 열이 있으면 1/true 인 사건만 쓴다 (S1·B·Q).
- --bars-dir: 코인마다 <SYMBOL>.csv, 열 t_open,open,high,low,close (15분봉, t_open = UTC ms)

사용
  탐색:      python run_trail.py --label E1 --events e1_events.csv --bars-dir bars_e1 --nmin 200
  판정 세기: python run_trail.py --label J --events h_events.csv --bars-dir bars_j --after "2026-10-10 13:00" --count-only
  판정:      python run_trail.py --label J --events h_events.csv --bars-dir bars_j --after "2026-10-10 13:00" --cutoff "<세기 때 시각>" --nmin 30
캡처 시간(KST 12:55~13:35, 20:55~21:35, 23:55~00:35)에는 실행하지 않는다.
"""
import os
import csv
import sys
import json
import bisect
import argparse
import datetime as dt
import trail_sim as T

KST = dt.timezone(dt.timedelta(hours=9))
CAPTURE = [((12, 55), (13, 35)), ((20, 55), (21, 35)), ((23, 55), (24, 0)), ((0, 0), (0, 35))]


def in_capture(now=None):
    n = now or dt.datetime.now(KST)
    m = n.hour * 60 + n.minute
    return any(a[0] * 60 + a[1] <= m < b[0] * 60 + b[1] for a, b in CAPTURE)


def kst_ms(s):
    if s is None:
        return None
    if s.isdigit():
        return int(s)
    return int(dt.datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=KST).timestamp() * 1000)


def load_events(path, after_ms):
    out = []
    with open(path, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if "cats_H3" in r and str(r["cats_H3"]).strip().lower() not in ("1", "true"):
                continue
            t = int(float(r["t_sig"]))
            if after_ms is not None and t <= after_ms:
                continue
            out.append({"event_id": r["event_id"], "symbol": r["symbol"], "t_sig": t,
                        "limit_px": float(r["limit_px"]), "limit_until": int(float(r["limit_until"]))})
    return out


_cache = {}


def bars_of(bars_dir, sym):
    if sym not in _cache:
        p = os.path.join(bars_dir, f"{sym}.csv")
        rows = []
        if os.path.exists(p):
            with open(p, encoding="utf-8-sig") as f:
                for r in csv.DictReader(f):
                    rows.append((int(float(r["t_open"])), float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"])))
        rows.sort()
        _cache[sym] = (rows, [r[0] for r in rows])
    return _cache[sym]


def window(bars_dir, e):
    rows, ts = bars_of(bars_dir, e["symbol"])
    a = bisect.bisect_left(ts, e["t_sig"])
    end = e["limit_until"] + max(T.HOLD_H.values()) * T.H + 2 * T.BAR_MS
    b = bisect.bisect_left(ts, end)
    return rows[a:b]


def fmt(x, p=2):
    return "-" if x is None else f"{x:+.{p}f}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True)
    ap.add_argument("--events", required=True)
    ap.add_argument("--bars-dir", required=True)
    ap.add_argument("--nmin", type=int, default=200)
    ap.add_argument("--after", help="t_sig 이 이 시각(KST 'YYYY-MM-DD HH:MM' 또는 ms)보다 뒤인 사건만")
    ap.add_argument("--cutoff", help="보유 기간이 이 시각까지 끝난 거래만 (판정용)")
    ap.add_argument("--count-only", action="store_true", help="수익률 없이 끝난 거래 수만 센다")
    ap.add_argument("--out", default=os.path.dirname(os.path.abspath(__file__)))
    a = ap.parse_args()
    if in_capture():
        sys.exit("캡처 시간대 — 끝난 뒤 실행")
    after = kst_ms(a.after)
    now_ms = int(dt.datetime.now(KST).timestamp() * 1000)
    cutoff = kst_ms(a.cutoff) if a.cutoff else (now_ms if a.count_only else None)
    evs = load_events(a.events, after)
    no_bars = sum(1 for e in evs if not bars_of(a.bars_dir, e["symbol"])[0])

    if a.count_only:
        n_fill, done = 0, {"H3": 0, "H4": 0}
        for e in evs:
            w = window(a.bars_dir, e)
            i = T.find_fill(w, e["t_sig"], e["limit_px"], e["limit_until"]) if w else None
            if i is None:
                continue
            n_fill += 1
            ft = w[i][0] + T.BAR_MS
            for s in done:
                done[s] += T.window_done(ft, s, cutoff)
        msg = {"label": a.label, "counted_at_kst": dt.datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S"),
               "cutoff_ms": cutoff, "after_ms": after, "events": len(evs), "no_bars": no_bars, "filled": n_fill,
               "done_H3": done["H3"], "done_H4": done["H4"], "nmin": a.nmin,
               "judge_H3": "가능" if done["H3"] >= a.nmin else "판정 불가", "judge_H4": "가능" if done["H4"] >= a.nmin else "판정 불가"}
        with open(os.path.join(a.out, f"count_{a.label}.json"), "w", encoding="utf-8") as f:
            json.dump(msg, f, ensure_ascii=False, indent=1)
        print(json.dumps(msg, ensure_ascii=False, indent=1))
        return

    trades = []
    for e in evs:
        w = window(a.bars_dir, e)
        r = T.simulate_event(w, e["t_sig"], e["limit_px"], e["limit_until"]) if w else None
        if r:
            trades.append({**e, **r})
    trades.sort(key=lambda t: t["fill_t"])
    res = {"label": a.label, "events": len(evs), "no_bars": no_bars, "filled": len(trades), "nmin": a.nmin, "variants": {}, "pairs": {}}
    sel = {}
    for s in ("H3", "H4"):
        ok = [t for t in trades if cutoff is None or T.window_done(t["fill_t"], s, cutoff)]
        sel[s] = ok
        res[f"cut_{s}"] = sum("CUT" in t["why"][f"{s}-현행"] for t in ok)
    for v in T.VARIANTS:
        res["variants"][v] = T.stats([t["ret"][v] for t in sel[v[:2]]])
    for v, c in T.PAIRS:
        ts = sel[v[:2]]
        res["pairs"][v] = T.compare([t["ret"][v] for t in ts], [t["ret"][c] for t in ts], a.nmin)

    with open(os.path.join(a.out, f"trades_{a.label}.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["event_id", "symbol", "t_sig", "fill_t"] + [f"ret_{v}" for v in T.VARIANTS] + [f"why_{v}" for v in T.VARIANTS])
        for t in trades:
            w.writerow([t["event_id"], t["symbol"], t["t_sig"], t["fill_t"]] + [f"{t['ret'][v]:.6f}" for v in T.VARIANTS]
                       + [t["why"][v] for v in T.VARIANTS])
    with open(os.path.join(a.out, f"summary_{a.label}.json"), "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1)
    kind = "판정" if a.label.upper().startswith("J") else "탐색"
    L = [f"# trail1014 {a.label} ({kind})", "",
         f"사건 {len(evs)} · 봉 없음 {no_bars} · 체결 {len(trades)} · 자료 끊김 H3 {res['cut_H3']} / H4 {res['cut_H4']} · 기준 D: N ≥ {a.nmin}", "",
         "| 변형 | N | 승률% | 평균% | 상위3 제외 평균% | 앞 절반% | 뒤 절반% |", "|---|---|---|---|---|---|---|"]
    for v in T.VARIANTS:
        s = res["variants"][v]
        win = "-" if s["win"] is None else f"{s['win']:.1f}"
        L.append(f"| {v} | {s['n']} | {win} | {fmt(s['mean'])} | {fmt(s['mean_ex_top3'])} | {fmt(s['mean_first'])} | {fmt(s['mean_second'])} |")
    L += ["", "| 비교 | N | 평균 Δ%p | Δ상위3 제외 | 앞 Δ | 뒤 Δ | z(참고) | A | B | C | D | 통과 |", "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for v, c in T.PAIRS:
        p = res["pairs"][v]
        yn = lambda b: "O" if b else "X"
        L.append(f"| {v} − {c} | {p['n']} | {fmt(p['d_mean'], 3)} | {fmt(p['d_ex_top3'], 3)} | {fmt(p['d_first'], 3)} | {fmt(p['d_second'], 3)} | "
                 f"{fmt(p['z'])} | {yn(p['A'])} | {yn(p['B'])} | {yn(p['C'])} | {yn(p['D'])} | {'통과' if p['pass'] else '불통과'} |")
    with open(os.path.join(a.out, f"table_{a.label}.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    print("\n".join(L))


if __name__ == "__main__":
    main()
