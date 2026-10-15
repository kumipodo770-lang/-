"""
prefill1015 실행 (PC에서). 규칙: prefill_study.md. 시뮬레이터·입력 형식·도우미는 옆 폴더 ../trail1014 것을 그대로 쓴다.
사용 (캡처 시간에는 실행 거부)
  탐색:      python run_prefill.py --mode explore --label E1 --events e1_events.csv --bars-dir bars_e1 --cutoff "<자료 끝 KST>"
  판정 세기: python run_prefill.py --mode judge --label J --events h.csv --bars-dir bars_j --after "2026-10-10 13:00" --count-only
  판정:      python run_prefill.py --mode judge --label J --events h.csv --bars-dir bars_j --after "2026-10-10 13:00" \
                 --cutoff <세기 결과의 cutoff_ms> --variants P-H3,P-H4
"""
import os
import sys
import csv
import json
import argparse
import datetime as dt

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "trail1014"))
import trail_sim as T          # noqa: E402
import run_trail as R          # noqa: E402

TP = 1.12
HOLD = {"P-H3": 72, "P-H4": 168, "P-H5": 168}


def touched_before_fill(bars, i_fill, t_sig, limit):
    """체결 봉 바로 앞 봉까지 고가가 익절선(지정가 × 1.12) 이상인 봉이 있었나 (체결 봉은 안 봄)"""
    lvl = limit * TP * (1 - T.EPS)
    return any(b[2] >= lvl for b in bars[:i_fill] if b[0] >= t_sig)


def current_returns(bars, e):
    """현행 H3·H4·H5 수익률과 이유. 체결 없으면 None."""
    i = T.find_fill(bars, e["t_sig"], e["limit_px"], e["limit_until"])
    if i is None:
        return None
    r = T.simulate_event(bars, e["t_sig"], e["limit_px"], e["limit_until"], names=["H3-현행", "H4-현행"])
    x, why, _ = T.run_leg(bars, i, e["limit_px"], 168, trig_pct=5)
    return {"fill_t": r["fill_t"], "skip": touched_before_fill(bars, i, e["t_sig"], e["limit_px"]),
            "ret": {"P-H3": r["ret"]["H3-현행"], "P-H4": r["ret"]["H4-현행"], "P-H5": T.ret_pct(e["limit_px"], x)},
            "why": {"P-H3": r["why"]["H3-현행"], "P-H4": r["why"]["H4-현행"], "P-H5": why}}


def mean(xs):
    return sum(xs) / len(xs) if xs else None


def evaluate(trades, v, nmin, smin):
    """trades: 체결 시각 순. 변형 v 의 비교 (prefill_study.md 4·5절)"""
    cur = [t["ret"][v] for t in trades]
    var = [0.0 if t["skip"] else t["ret"][v] for t in trades]
    c = T.compare(var, cur, nmin)
    sk = [t["ret"][v] for t in trades if t["skip"]]
    kept = [t["ret"][v] for t in trades if not t["skip"]]
    c.update(n_skip=len(sk), skip_share=(len(sk) / len(cur) * 100) if cur else None, mean_all=mean(cur), mean_skipped=mean(sk),
             mean_kept=mean(kept), win_all=(sum(x > 0 for x in cur) / len(cur) * 100) if cur else None,
             win_kept=(sum(x > 0 for x in kept) / len(kept) * 100) if kept else None)
    c["D"] = c["n"] >= nmin and len(sk) >= smin
    c["pass"] = c["A"] and c["B"] and c["C"] and c["D"]
    return c


def done(ft, v, cutoff):
    return ft + HOLD[v] * T.H <= cutoff


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", required=True, choices=["explore", "judge"])
    ap.add_argument("--label", required=True)
    ap.add_argument("--events", required=True)
    ap.add_argument("--bars-dir", required=True)
    ap.add_argument("--after")
    ap.add_argument("--cutoff")
    ap.add_argument("--variants", help="judge 에서 계산할 변형 (탐색 통과한 것만), 쉼표로")
    ap.add_argument("--count-only", action="store_true")
    ap.add_argument("--out", default=HERE)
    a = ap.parse_args()
    if R.in_capture():
        sys.exit("캡처 시간대 — 끝난 뒤 실행")
    judge = a.mode == "judge"
    kind = "판정" if judge else "탐색"
    nmin, smin = (30, 10) if judge else (200, 20)
    after = R.kst_ms(a.after)
    cutoff = R.kst_ms(a.cutoff) if a.cutoff else None
    if cutoff is None:
        if a.count_only and judge:
            cutoff = int(dt.datetime.now(R.KST).timestamp() * 1000)
        else:
            sys.exit("--cutoff 필요 (explore: 자료 끝 시각, judge: 세기 때 cutoff)")
    chosen = list(HOLD)
    if judge and not a.count_only:
        chosen = [v.strip() for v in (a.variants or "").split(",") if v.strip()]
        if not chosen or any(v not in HOLD for v in chosen):
            sys.exit(f"judge 에는 --variants 필요 ({', '.join(HOLD)} 중 탐색 통과한 것)")
    evs, dup = R.load_events(a.events, after)
    head = {"mode": a.mode, "kind": kind, "label": a.label, "at_kst": R.stamp(), "cutoff_ms": cutoff, "after_ms": after,
            "events": len(evs), "dup_events_dropped": dup, "nmin": nmin, "smin": smin}

    # 표본 먼저 센다 (체결 여부·시각·체결 전 고가만 — 수익률 없이)
    fills = []
    for e in evs:
        w = R.window(a.bars_dir, e)
        i = T.find_fill(w, e["t_sig"], e["limit_px"], e["limit_until"]) if w else None
        if i is not None:
            fills.append((e, w, w[i][0] + T.BAR_MS, touched_before_fill(w, i, e["t_sig"], e["limit_px"])))
    counts = {v: {"n": sum(done(ft, v, cutoff) for _, _, ft, _ in fills),
                  "n_skip": sum(done(ft, v, cutoff) and sk for _, _, ft, sk in fills)} for v in HOLD}
    if a.count_only:
        msg = {**head, "filled": len(fills), "counts": counts,
               "status": {v: ("판정 가능" if c["n"] >= nmin and c["n_skip"] >= smin else "판정 불가") for v, c in counts.items()}}
        fn = f"count_{a.label}_{head['at_kst'][:16].replace(' ', '_').replace(':', '')}.json"
        with open(os.path.join(a.out, fn), "w", encoding="utf-8") as f:
            json.dump(msg, f, ensure_ascii=False, indent=1)
        print(json.dumps(msg, ensure_ascii=False, indent=1))
        return

    run = [v for v in chosen if not (judge and (counts[v]["n"] < nmin or counts[v]["n_skip"] < smin))]
    res = {**head, "filled": len(fills), "counts": counts, "results": {},
           "not_judged": {v: "판정 불가 (표본 부족), 수익률 계산 안 함" for v in chosen if v not in run}}
    trades = []
    if run:
        for e, w, ft, sk in fills:
            if any(done(ft, v, cutoff) for v in run):
                r = current_returns(w, e)
                trades.append({**e, **r})
        trades.sort(key=T.sort_key)
    for v in run:
        ts = [t for t in trades if done(t["fill_t"], v, cutoff)]
        cuts = sum("CUT" in t["why"][v] for t in ts)
        if judge and cuts:
            sys.exit(f"판정 표본에 자료 끊김 {cuts}건 → 봉 자료를 cutoff 까지 다시 받은 뒤 실행 (결과 내지 않음)")
        res["results"][v] = {**evaluate(ts, v, nmin, smin), "cut": cuts}

    with open(os.path.join(a.out, f"trades_{a.label}.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["mode", "event_id", "symbol", "t_sig", "fill_t", "skip"] + [f"ret_{v}" for v in HOLD] + [f"why_{v}" for v in HOLD])
        for t in trades:
            w.writerow([a.mode, t["event_id"], t["symbol"], t["t_sig"], t["fill_t"], int(t["skip"])]
                       + [f"{t['ret'][v]:.6f}" for v in HOLD] + [t["why"][v] for v in HOLD])
    with open(os.path.join(a.out, f"summary_{a.label}.json"), "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1)
    fm = R.fmt
    L = [f"# prefill1015 {a.label} ({kind})", "", f"사건 {len(evs)} (중복 제거 {dup}) · 체결 {len(fills)} · cutoff {cutoff} · "
         f"기준 D: N ≥ {nmin}, 건너뛴 사건 ≥ {smin}", ""]
    for v, why in res["not_judged"].items():
        L.append(f"- {v}: {why} (N {counts[v]['n']}, 건너뛸 사건 {counts[v]['n_skip']})")
    L += ["", "| 변형 | N | 건너뜀 (비율) | 전체 평균% | 남은 평균% | 건너뛴 평균% | 승률 전체/남은 | 평균 Δ%p | Δ상위3 제외 | 앞 Δ | 뒤 Δ | z | A | B | C | D | 결과 |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    yn = lambda b: "O" if b else "X"
    for v, c in res["results"].items():
        sh = "-" if c["skip_share"] is None else f"{c['skip_share']:.1f}%"
        wa = "-" if c["win_all"] is None else f"{c['win_all']:.0f}"
        wk = "-" if c["win_kept"] is None else f"{c['win_kept']:.0f}"
        L.append(f"| {v} | {c['n']} | {c['n_skip']} ({sh}) | {fm(c['mean_all'])} | {fm(c['mean_kept'])} | {fm(c['mean_skipped'])} | {wa}/{wk} | "
                 f"{fm(c['d_mean'], 3)} | {fm(c['d_ex_top3'], 3)} | {fm(c['d_first'], 3)} | {fm(c['d_second'], 3)} | {fm(c['z'])} | "
                 f"{yn(c['A'])} | {yn(c['B'])} | {yn(c['C'])} | {yn(c['D'])} | {kind} {'통과' if c['pass'] else '불통과'} |")
    with open(os.path.join(a.out, f"table_{a.label}.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    print("\n".join(L))


if __name__ == "__main__":
    main()
