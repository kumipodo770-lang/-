"""
trail1014 실행 (PC에서). 결과는 이 폴더(= backend\\out in\\trail1014\\)에 저장. 규칙: trail_study.md + 변경 기록.

입력
- --events CSV: event_id,symbol,t_sig,limit_px,limit_until,cats_H3[,c0]  (시각은 UTC ms, t_sig = 신호 15분봉 마감)
  cats_H3(또는 cats.H3) 열이 꼭 있어야 하고 참(1/1.0/true/yes)인 사건만 쓴다 (S1·B·Q).
  limit_until 은 t_sig + 24h 여야 하고, c0 열이 있으면 limit_px = c0 × 0.95 인지도 본다.
- --bars-dir: 코인마다 <SYMBOL>.csv, 열 t_open,open,high,low,close (15분봉, t_open = UTC ms)

사용
  탐색:      python run_trail.py --mode explore --label E1 --events e1.csv --bars-dir bars_e1 --cutoff "<자료 끝 KST>"
  판정 세기: python run_trail.py --mode judge --label J --events h.csv --bars-dir bars_j --after "2026-10-10 13:00" --count-only
  판정:      python run_trail.py --mode judge --label J --events h.csv --bars-dir bars_j --after "2026-10-10 13:00" \
                 --cutoff <세기 결과의 cutoff_ms> --variants H3-L5,H4-V1
  (판정 변형은 탐색에서 고른 것만, H3 하나·H4 하나까지)
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
TRUE = ("1", "1.0", "true", "yes")


def in_capture(now=None):
    n = now or dt.datetime.now(KST)
    m = n.hour * 60 + n.minute
    return any(a[0] * 60 + a[1] <= m < b[0] * 60 + b[1] for a, b in CAPTURE)


def kst_ms(s):
    if s is None:
        return None
    if s.isdigit():
        return int(s)
    for f in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return int(dt.datetime.strptime(s, f).replace(tzinfo=KST).timestamp() * 1000)
        except ValueError:
            pass
    sys.exit(f"시각 형식을 모름: {s}")


def load_events(path, after_ms):
    out, seen, dup = [], set(), 0
    with open(path, encoding="utf-8-sig") as f:
        rd = csv.DictReader(f)
        col = "cats_H3" if "cats_H3" in rd.fieldnames else "cats.H3" if "cats.H3" in rd.fieldnames else None
        if not col:
            sys.exit("events CSV 에 cats_H3 열이 없음 (B·Q 통과 여부 필요)")
        for r in rd:
            if str(r[col]).strip().lower() not in TRUE:
                continue
            t = int(float(r["t_sig"]))
            if after_ms is not None and t <= after_ms:
                continue
            e = {"event_id": r["event_id"], "symbol": r["symbol"], "t_sig": t,
                 "limit_px": float(r["limit_px"]), "limit_until": int(float(r["limit_until"]))}
            if e["limit_until"] - t != 24 * T.H:
                sys.exit(f"{e['event_id']}: limit_until 이 t_sig + 24h 가 아님")
            if r.get("c0") and abs(e["limit_px"] - float(r["c0"]) * 0.95) > 1e-9 * e["limit_px"]:
                sys.exit(f"{e['event_id']}: limit_px 가 c0 × 0.95 가 아님")
            k = (e["symbol"], t)
            if k in seen:
                dup += 1
                continue
            seen.add(k)
            out.append(e)
    return out, dup


_cache = {}


def bars_of(bars_dir, sym):
    if sym not in _cache:
        p = os.path.join(bars_dir, f"{sym}.csv")
        rows = {}
        if os.path.exists(p):
            with open(p, encoding="utf-8-sig") as f:
                for r in csv.DictReader(f):
                    t = int(float(r["t_open"]))
                    rows.setdefault(t, (t, float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"])))
        rows = sorted(rows.values())
        _cache[sym] = (rows, [r[0] for r in rows])
    return _cache[sym]


def window(bars_dir, e):
    rows, ts = bars_of(bars_dir, e["symbol"])
    a = bisect.bisect_left(ts, e["t_sig"])
    end = e["limit_until"] + max(T.HOLD_H.values()) * T.H + 2 * T.BAR_MS
    return rows[a:bisect.bisect_left(ts, end)]


def fmt(x, p=2):
    return "-" if x is None else f"{x:+.{p}f}"


def stamp():
    return dt.datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", required=True, choices=["explore", "judge"])
    ap.add_argument("--label", required=True)
    ap.add_argument("--events", required=True)
    ap.add_argument("--bars-dir", required=True)
    ap.add_argument("--nmin", type=int, help="기준 D (기본: explore 200, judge 30)")
    ap.add_argument("--after", help="t_sig 이 이 시각(KST 'YYYY-MM-DD HH:MM[:SS]' 또는 ms)보다 뒤인 사건만")
    ap.add_argument("--cutoff", help="보유가 이 시각까지 끝나는 거래만. explore: 자료 끝, judge: 세기 때 cutoff")
    ap.add_argument("--variants", help="judge 에서 계산할 변형 (H3 하나·H4 하나까지), 쉼표로")
    ap.add_argument("--count-only", action="store_true", help="수익률 없이 끝난 거래 수만 센다")
    ap.add_argument("--out", default=os.path.dirname(os.path.abspath(__file__)))
    a = ap.parse_args()
    if in_capture():
        sys.exit("캡처 시간대 — 끝난 뒤 실행")
    judge = a.mode == "judge"
    kind = "판정" if judge else "탐색"
    nmin = a.nmin or (30 if judge else 200)
    after = kst_ms(a.after)
    cutoff = kst_ms(a.cutoff) if a.cutoff else None
    if cutoff is None:
        if a.count_only and judge:
            cutoff = int(dt.datetime.now(KST).timestamp() * 1000)
        else:
            sys.exit("--cutoff 필요 (explore: 자료 끝 시각, judge: 세기 때 cutoff)")
    evs, dup = load_events(a.events, after)
    no_bars = sum(1 for e in evs if not bars_of(a.bars_dir, e["symbol"])[0])
    head = {"mode": a.mode, "kind": kind, "label": a.label, "at_kst": stamp(), "cutoff_ms": cutoff, "after_ms": after,
            "events": len(evs), "dup_events_dropped": dup, "no_bars": no_bars, "nmin": nmin}

    if a.count_only:
        n_fill, done = 0, {"H3": 0, "H4": 0}
        for e in evs:
            w = window(a.bars_dir, e)
            i = T.find_fill(w, e["t_sig"], e["limit_px"], e["limit_until"]) if w else None
            if i is None:
                continue
            n_fill += 1
            for s in done:
                done[s] += T.window_done(w[i][0] + T.BAR_MS, s, cutoff)
        msg = {**head, "filled": n_fill, "done_H3": done["H3"], "done_H4": done["H4"],
               "H3": "판정 가능" if done["H3"] >= nmin else "판정 불가", "H4": "판정 가능" if done["H4"] >= nmin else "판정 불가"}
        fn = f"count_{a.label}_{head['at_kst'][:16].replace(' ', '_').replace(':', '')}.json"
        with open(os.path.join(a.out, fn), "w", encoding="utf-8") as f:
            json.dump(msg, f, ensure_ascii=False, indent=1)
        print(json.dumps(msg, ensure_ascii=False, indent=1))
        return

    if judge:
        if not a.variants:
            sys.exit("judge 에는 --variants 필요 (탐색에서 고른 H3 하나·H4 하나까지)")
        chosen = [v.strip() for v in a.variants.split(",") if v.strip()]
        if any(v not in T.BASE for v in chosen) or len({v[:2] for v in chosen}) != len(chosen):
            sys.exit(f"--variants 는 {list(T.BASE)} 중 전략마다 하나까지")
    else:
        chosen = list(T.BASE)
    names = sorted({x for v in chosen for x in (v, T.BASE[v])}, key=T.VARIANTS.index)

    # 표본 먼저 센다 (수익률 없이) → 판정에서 N < nmin 인 전략은 계산하지 않음
    fills = []
    for e in evs:
        w = window(a.bars_dir, e)
        i = T.find_fill(w, e["t_sig"], e["limit_px"], e["limit_until"]) if w else None
        if i is not None:
            fills.append((e, w, w[i][0] + T.BAR_MS))
    res = {**head, "filled": len(fills), "strategies": {}, "variants": {}, "pairs": {}}
    run = {}
    for s in ("H3", "H4"):
        n = sum(T.window_done(ft, s, cutoff) for _, _, ft in fills)
        res["strategies"][s] = {"n": n}
        if not any(v[:2] == s for v in chosen):
            res["strategies"][s]["status"] = "대상 아님"
        elif judge and n < nmin:
            res["strategies"][s]["status"] = "판정 불가 (N < %d), 수익률 계산 안 함" % nmin
        else:
            run[s] = [x for x in names if x[:2] == s]

    trades = []
    for e, w, ft in fills:
        todo = [x for s, xs in run.items() if T.window_done(ft, s, cutoff) for x in xs]
        if todo:
            trades.append({**e, **T.simulate_event(w, e["t_sig"], e["limit_px"], e["limit_until"], names=todo)})
    trades.sort(key=T.sort_key)
    for s, xs in run.items():
        ts = [t for t in trades if xs[0] in t["ret"]]
        cuts = {x: sum("CUT" in t["why"][x] for t in ts) for x in xs}
        res["strategies"][s]["cut"] = cuts
        if judge and any(cuts.values()):
            bad = sorted({t["event_id"] for t in ts for x in xs if "CUT" in t["why"][x]})
            sys.exit(f"판정 표본에 자료 끊김 {len(bad)}건 → 봉 자료를 cutoff 까지 다시 받은 뒤 실행 (결과 내지 않음): {bad[:10]}")
        for x in xs:
            res["variants"][x] = T.stats([t["ret"][x] for t in ts])
        for v in chosen:
            if v[:2] == s:
                res["pairs"][v] = T.compare([t["ret"][v] for t in ts], [t["ret"][T.BASE[v]] for t in ts], nmin)

    with open(os.path.join(a.out, f"trades_{a.label}.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["mode", "event_id", "symbol", "t_sig", "fill_t"] + [f"ret_{x}" for x in names] + [f"why_{x}" for x in names])
        for t in trades:
            w.writerow([a.mode, t["event_id"], t["symbol"], t["t_sig"], t["fill_t"]]
                       + [f"{t['ret'][x]:.6f}" if x in t["ret"] else "" for x in names] + [t["why"].get(x, "") for x in names])
    with open(os.path.join(a.out, f"summary_{a.label}.json"), "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1)
    L = [f"# trail1014 {a.label} ({kind})", "",
         f"사건 {len(evs)} (중복 제거 {dup}) · 봉 없음 {no_bars} · 체결 {len(fills)} · 기준 D: N ≥ {nmin} · cutoff {cutoff}", ""]
    for s, st in res["strategies"].items():
        L.append(f"- {s}: N {st['n']} · {st.get('status', '계산')}" + (f" · 자료 끊김 {st['cut']}" if "cut" in st else ""))
    L += ["", "| 변형 | N | 승률% | 평균% | 상위3 제외 평균% | 앞 절반% | 뒤 절반% |", "|---|---|---|---|---|---|---|"]
    for x, s in res["variants"].items():
        win = "-" if s["win"] is None else f"{s['win']:.1f}"
        L.append(f"| {x} | {s['n']} | {win} | {fmt(s['mean'])} | {fmt(s['mean_ex_top3'])} | {fmt(s['mean_first'])} | {fmt(s['mean_second'])} |")
    L += ["", "| 비교 | N | 평균 Δ%p | Δ상위3 제외 | 앞 Δ | 뒤 Δ | z(참고) | A | B | C | D | 결과 |", "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    yn = lambda b: "O" if b else "X"
    for v, p in res["pairs"].items():
        L.append(f"| {v} − {T.BASE[v]} | {p['n']} | {fmt(p['d_mean'], 3)} | {fmt(p['d_ex_top3'], 3)} | {fmt(p['d_first'], 3)} | "
                 f"{fmt(p['d_second'], 3)} | {fmt(p['z'])} | {yn(p['A'])} | {yn(p['B'])} | {yn(p['C'])} | {yn(p['D'])} | "
                 f"{kind} {'통과' if p['pass'] else '불통과'} |")
    with open(os.path.join(a.out, f"table_{a.label}.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    print("\n".join(L))


if __name__ == "__main__":
    main()
