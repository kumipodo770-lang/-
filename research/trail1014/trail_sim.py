"""
trail1014 시뮬레이터 — trail_study.md (사전 고정) 2·3·4절 규칙 그대로. 15분봉, 순수 함수.
봉 = (t_open_ms, open, high, low, close), t_open 오름차순.
"""
BAR_MS = 15 * 60 * 1000
H = 3_600_000
FEE_MAKER = 0.0002
FEE_TAKER = 0.00055
STOP = 0.85          # 손절가 = 진입가(=지정가) × 0.85
LOCK = 1.005         # 잠금 손절가 = 진입가 × 1.005
H4_TP = 12.0

VARIANTS = ["H3-현행", "H3-L3", "H3-L5", "H3-L8", "H4-현행", "H4-V1", "H4-V2"]
PAIRS = [("H3-L3", "H3-현행"), ("H3-L5", "H3-현행"), ("H3-L8", "H3-현행"),
         ("H4-V1", "H4-현행"), ("H4-V2", "H4-현행")]
HOLD_H = {"H3": 72, "H4": 168}


def find_fill(bars, t_sig, limit_px, limit_until):
    """t_sig 이후 시작하는 봉 중 저가 < 지정가인 첫 봉의 인덱스 (24h 안). 없으면 None."""
    for i, (t, o, h, l, c) in enumerate(bars):
        if t < t_sig:
            continue
        if t >= limit_until:
            return None
        if l < limit_px:
            return i
    return None


def ret_pct(entry, exit_px):
    """수익률(%) — 진입 메이커, 청산 테이커. 펀딩·슬리피지 없음."""
    return ((exit_px - entry) - entry * FEE_MAKER - exit_px * FEE_TAKER) / entry * 100


def run_leg(bars, i_fill, entry, hold_h, tp_pct=None, trig_pct=None, arm_from=None):
    """다리 하나. 반환 (청산가, 이유, 청산 봉 인덱스). 이유: SL / LOCK / TP / TIME / CUT.
    trig_pct: 고가가 +trig% 닿으면 다음 봉부터 손절 = 진입가×1.005.
    arm_from: 이 봉 인덱스부터 손절 = 진입가×1.005 (H4 현행 b다리: a 익절 봉 + 1)."""
    stop = entry * STOP
    lock = entry * LOCK
    tp = entry * (1 + tp_pct / 100) if tp_pct else None
    trig = entry * (1 + trig_pct / 100) if trig_pct else None
    t0, o, h, l, c = bars[i_fill]
    if l <= stop:                                   # 체결 봉: 손절만 본다 (불리)
        return min(stop, o), "SL", i_fill
    due = t0 + BAR_MS + hold_h * H                  # 체결 시각 = 체결 봉 마감
    cur, armed, arm_next = stop, False, None
    for j in range(i_fill + 1, len(bars)):
        t, o, h, l, c = bars[j]
        if t >= due:
            return o, "TIME", j
        if (arm_next is not None and j >= arm_next) or (arm_from is not None and j >= arm_from):
            if not armed:
                cur, armed = lock, True
        if l <= cur:                                # 1) 손절 먼저 (같은 봉 익절·트리거보다 우선)
            return min(cur, o), ("LOCK" if armed else "SL"), j
        if tp is not None and h >= tp:              # 2) 익절
            return max(tp, o), "TP", j
        if trig is not None and not armed and arm_next is None and h >= trig:
            arm_next = j + 1                        # 3) 트리거 → 다음 봉부터
    return bars[-1][4], "CUT", len(bars) - 1


def simulate_event(bars, t_sig, limit_px, limit_until):
    """사건 하나 → None (미체결) 또는 {fill_t, 변형별 수익률·이유}."""
    i = find_fill(bars, t_sig, limit_px, limit_until)
    if i is None:
        return None
    e = limit_px
    out = {"fill_t": bars[i][0] + BAR_MS, "ret": {}, "why": {}}

    def put(name, legs):
        out["ret"][name] = sum(w * ret_pct(e, x) for w, (x, _, _) in legs)
        out["why"][name] = "+".join(r for _, (_, r, _) in legs)

    put("H3-현행", [(1.0, run_leg(bars, i, e, 72))])
    for x in (3, 5, 8):
        put(f"H3-L{x}", [(1.0, run_leg(bars, i, e, 72, trig_pct=x))])
    a = run_leg(bars, i, e, 168, tp_pct=H4_TP)
    b_cur = run_leg(bars, i, e, 168, arm_from=(a[2] + 1) if a[1] == "TP" else None)
    put("H4-현행", [(0.5, a), (0.5, b_cur)])
    put("H4-V1", [(1.0, run_leg(bars, i, e, 168, trig_pct=H4_TP))])
    put("H4-V2", [(0.5, a), (0.5, run_leg(bars, i, e, 168))])
    return out


def window_done(fill_t, strategy, cutoff_ms):
    """보유 기간까지 끝났는지 (판정 표본 세기용, 수익률은 보지 않음)."""
    return fill_t + HOLD_H[strategy] * H <= cutoff_ms


# ---------------- 지표·기준 (5·6절) ----------------
def _mean(xs):
    return sum(xs) / len(xs) if xs else None


def _halves(xs):
    k = len(xs) // 2
    return xs[:k], xs[k:]


def stats(rets):
    """rets: 체결 시각 순. N, 승률, 평균, 상위3 제외 평균, 앞·뒤 절반 평균."""
    n = len(rets)
    first, second = _halves(rets)
    top3_ex = sorted(rets)[:-3] if n > 3 else []
    return {"n": n, "win": (sum(r > 0 for r in rets) / n * 100) if n else None, "mean": _mean(rets),
            "mean_ex_top3": _mean(top3_ex), "mean_first": _mean(first), "mean_second": _mean(second)}


def compare(var, cur, n_min):
    """var, cur: 같은 거래 순서(체결 시각 순)의 수익률. 기준 A~D."""
    d = [v - c for v, c in zip(var, cur)]
    n = len(d)
    first, second = _halves(d)
    ex = sorted(d)[:-3] if n > 3 else []
    md = _mean(d)
    sd = (sum((x - md) ** 2 for x in d) / (n - 1)) ** 0.5 if n > 1 else None
    z = md / (sd / n ** 0.5) if sd else None
    r = {"n": n, "d_mean": md, "d_ex_top3": _mean(ex), "d_first": _mean(first), "d_second": _mean(second), "z": z}
    r["A"] = md is not None and md > 0
    r["B"] = r["d_ex_top3"] is not None and r["d_ex_top3"] > 0
    r["C"] = r["d_first"] is not None and r["d_second"] is not None and r["d_first"] > 0 and r["d_second"] > 0
    r["D"] = n >= n_min
    r["pass"] = r["A"] and r["B"] and r["C"] and r["D"]
    return r
