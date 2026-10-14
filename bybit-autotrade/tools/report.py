"""
live_test_plan.md 4·5절 지표 계산 (읽기만). 사용: .venv\\Scripts\\python tools\\report.py [db 경로] [장부]
- 실행 차이 d = 실전 수익률 − 같은 사건 사이트 결과 (평균, 하루 단위 다시뽑기 95% 범위)
- 구성: 진입 체결가 − 지정가(bp), 수수료 %, 펀딩 %
- 체결 일치율: 사이트 지정가 체결 사건 중 봇 체결 비율 / 봇 체결 중 사이트 미체결 비율 (진입 결정한 사건만)
- 성공·실패 기준 판정 (청산 30건 이상일 때)
"""
import os
import sys
import json
import random
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone, timedelta

KST = timezone(timedelta(hours=9))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def day(ms):
    return datetime.fromtimestamp(ms / 1000, KST).strftime("%Y-%m-%d")


def boot_mean_ci(rows, key, n=2000, seed=0):
    by = defaultdict(list)
    for r in rows:
        by[day(r["exit_ts"])].append(r[key])
    days = list(by)
    if not days:
        return None, None, None
    rnd = random.Random(seed)
    means = []
    for _ in range(n):
        xs = [x for d in (rnd.choice(days) for _ in days) for x in by[d]]
        means.append(sum(xs) / len(xs))
    means.sort()
    allx = [r[key] for r in rows]
    return sum(allx) / len(allx), means[int(0.025 * n)], means[int(0.975 * n)]


def main():
    db = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "data", "bot.db")
    only = sys.argv[2] if len(sys.argv) > 2 else None
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    legs = [dict(r) for r in con.execute("SELECT * FROM legs")]
    sigs = {r["event_id"]: dict(r) for r in con.execute("SELECT * FROM signals")}
    decs = [dict(r) for r in con.execute("SELECT * FROM decisions")]
    books = sorted({l["book"] for l in legs} | {d["book"] for d in decs})
    for b in books:
        if only and b != only:
            continue
        print(f"\n===== 장부 {b} =====")
        bd = [d for d in decs if d["book"] == b]
        print(f"신호 결정 {len(bd)}건: 진입 {sum(d['decision'] == 'enter' for d in bd)} / 건너뜀 {sum(d['decision'] == 'skip' for d in bd)}")
        reasons = defaultdict(int)
        for d in bd:
            if d["decision"] == "skip":
                reasons[d["reason"].split("(")[0].strip()[:40]] += 1
        for k, v in sorted(reasons.items(), key=lambda x: -x[1])[:8]:
            print(f"  건너뜀 {v:4d}  {k}")
        for s in ("H3", "H4", "H5"):
            L = [l for l in legs if l["book"] == b and l["strategy"] == s]
            if not L:
                continue
            closed = [l for l in L if l["status"] == "closed" and l["pnl_pct"] is not None]
            # 전략 단위(H4 는 두 다리 합)로 사건별 수익률
            ev = defaultdict(lambda: {"pnl": 0.0, "notional": 0.0, "exit_ts": 0, "site": None, "slip": [], "fee": 0.0, "fund": 0.0})
            for l in closed:
                e = ev[l["event_id"]]
                n = l["fill_px"] * l["fill_qty"]
                e["pnl"] += l["pnl_usdt"]; e["notional"] += n; e["exit_ts"] = max(e["exit_ts"], l["exit_ts"])
                e["site"] = l["site_result"]; e["slip"].append((l["fill_px"] / l["limit_px"] - 1) * 1e4)
                e["fee"] += (l["entry_fee"] or 0) + (l["exit_fee"] or 0); e["fund"] += l["funding"] or 0
            done = [{"eid": k, "ret": v["pnl"] / v["notional"] * 100, "exit_ts": v["exit_ts"], "site": v["site"],
                     "slip": sum(v["slip"]) / len(v["slip"]), "fee": v["fee"] / v["notional"] * 100, "fund": v["fund"] / v["notional"] * 100}
                    for k, v in ev.items() if v["notional"] > 0]
            paired = [dict(x, d=x["ret"] - x["site"]) for x in done if x["site"] is not None]
            print(f"\n[{s}] 다리 {len(L)} (청산 {len(closed)}) · 사건 청산 {len(done)} · 사이트 짝 {len(paired)}")
            if done:
                m = sum(x["ret"] for x in done) / len(done)
                print(f"  실전 평균 {m:+.2f}% · 승률 {sum(x['ret'] > 0 for x in done) / len(done) * 100:.0f}% · 진입 슬리피지 평균 {sum(x['slip'] for x in done) / len(done):+.1f}bp"
                      f" · 수수료 {sum(x['fee'] for x in done) / len(done):.3f}% · 펀딩 {sum(x['fund'] for x in done) / len(done):+.3f}%")
            if paired:
                md, lo, hi = boot_mean_ci(paired, "d")
                sm = sum(x["site"] for x in paired) / len(paired)
                rm = sum(x["ret"] for x in paired) / len(paired)
                print(f"  실행 차이 d 평균 {md:+.2f}%p (95% {lo:+.2f} ~ {hi:+.2f}) · 같은 사건 사이트 평균 {sm:+.2f}% vs 실전 {rm:+.2f}%")
            # 체결 일치율 (진입 결정한 사건)
            ent = [d for d in bd if d["strategy"] == s and d["decision"] == "enter"]
            site_f = bot_f = both = 0
            for d in ent:
                sg = sigs.get(d["event_id"]) or {}
                lim = json.loads(sg.get("site_lim") or "{}")
                sf = lim.get("status") == "filled"
                bf = any(l["event_id"] == d["event_id"] and (l["fill_qty"] or 0) > 0 for l in L)
                site_f += sf; bot_f += bf; both += sf and bf
            if ent:
                r1 = both / site_f * 100 if site_f else float("nan")
                r2 = (bot_f - both) / bot_f * 100 if bot_f else float("nan")
                print(f"  체결 일치: 사이트 체결 {site_f} 중 봇 체결 {both} ({r1:.0f}%) · 봇 체결 {bot_f} 중 사이트 미체결 {bot_f - both} ({r2:.0f}%)")
            if len(paired) >= 30:
                md, lo, hi = boot_mean_ci(paired, "d")
                s1 = md >= -0.5 and lo >= -1.0
                s2 = site_f and both / site_f >= 0.9 and (bot_f - both) / max(bot_f, 1) <= 0.1
                f1, f2 = md < -1.0, site_f and both / site_f < 0.8
                verdict = "실패" if (f1 or f2) else ("성공(S1·S2)" if (s1 and s2) else "판정 보류")
                print(f"  ▶ 계획 5절 판정 (n={len(paired)}): {verdict}  [S1 {s1} S2 {bool(s2)} F1 {f1} F2 {bool(f2)}] — S3·S4 는 따로 확인")
        ev_err = con.execute("SELECT kind, COUNT(*) c FROM events WHERE book=? AND level='ERROR' GROUP BY kind", (b,)).fetchall()
        if ev_err:
            print("  오류: " + ", ".join(f"{r['kind']} {r['c']}" for r in ev_err))


if __name__ == "__main__":
    main()
