"""
리스크 모듈 — 전략 코드와 분리. 모든 신규 진입 주문은 check_entry 를 통과해야 나간다 (엔진이 강제).
청산(감소 전용)은 check_reduce 만 본다 — 정지 상태에서도 청산은 허용.

규칙 (값은 config.yaml risk 절에서만 바뀜):
 1. 정지: 수동 정지 / 누적 손실 정지 / API 실패로 진입 막힘 → 거부
 2. 일일 손실: 오늘(KST) 실현 손실이 그 전략 예산의 daily_loss_limit_pct 이상 → 그 전략만 그날 신규 진입 거부 (전략마다 독립)
 3. 누적 손실: 기준 시점 이후 실현 손익이 그 전략 예산의 −cumulative_stop_pct 이하 → 그 전략 정지 (재개는 설정 파일 숫자 변경으로만)
 4. 중복: 같은 사건·전략은 한 번만. 같은 전략이 그 코인을 이미 들고 있으면 거부. 봇 밖 포지션이 있는 코인은 거부
 5. 손절 필수, 손절 시 손실(수수료 포함) ≤ 예산 × per_trade_risk_cap_pct
 6. 레버리지: 청산가가 손절가보다 liq_buffer_pct(%p) 이상 멀도록 계산, 상한 leverage_cap. 안 되면 거부
 7. 최소 주문(수량·금액)·수량 단위, 동시 포지션 수, 증거금 합 ≤ 예산
"""
import math
from dataclasses import dataclass, field


@dataclass
class LegPlan:
    leg: str
    qty: float
    tp: float | None = None


@dataclass
class EntryIntent:
    book: str
    mode: str
    strategy: str
    event_id: str
    symbol: str
    price: float
    sl: float
    legs: list            # [LegPlan]
    leverage: int | None = None   # None = 리스크 모듈이 정함

    @property
    def qty(self):
        return sum(l.qty for l in self.legs)


@dataclass
class RiskState:
    halted_global: str | None = None
    halted_strategy: dict = field(default_factory=dict)     # strategy → 이유
    entries_blocked: str | None = None
    realized_today: dict = field(default_factory=dict)      # strategy → USDT (손실은 음수)
    realized_cum: dict = field(default_factory=dict)        # strategy → USDT (기준 시점 이후)
    open_events: dict = field(default_factory=dict)         # strategy → {event_id: {symbol, margin}}
    decided: set = field(default_factory=set)               # {(event_id, strategy)}
    foreign_symbols: set = field(default_factory=set)


@dataclass
class Decision:
    ok: bool
    reason: str
    leverage: int = 0
    margin: float = 0.0
    risk_usdt: float = 0.0
    risk_pct: float = 0.0


def _dec(step):
    from decimal import Decimal
    return max(0, -Decimal(str(step)).normalize().as_tuple().exponent)


def floor_step(x, step):
    return round(math.floor(x / step + 1e-9) * step, _dec(step))


def ceil_step(x, step):
    return round(math.ceil(x / step - 1e-9) * step, _dec(step))


class RiskManager:
    def __init__(self, rcfg: dict, budgets: dict, mode: str):
        self.r = rcfg
        self.budgets = {k: float(v or 0) for k, v in (budgets or {}).items()}
        self.mode = mode

    # ---------- 계산 도우미 ----------
    def loss_at_sl(self, qty, price, sl):
        ft = self.r["fee_taker_pct"] / 100
        return qty * (price - sl) + qty * price * ft + qty * sl * ft

    def choose_leverage(self, price, sl, mmr, max_lev):
        """청산 거리(≈ 1/L − mmr) ≥ 손절 거리 + 여유 를 만족하는 가장 큰 정수 L (상한 leverage_cap)"""
        sl_dist = (price - sl) / price
        need = sl_dist + self.r["liq_buffer_pct"] / 100 + mmr
        if need >= 1:
            return None
        L = math.floor(1 / need + 1e-9)
        L = min(L, int(self.r["leverage_cap"]), int(max_lev or 1))
        return L if L >= 1 else None

    def liq_ok(self, price, sl, L, mmr):
        if L < 1 or L > self.r["leverage_cap"]:
            return False
        liq = price * (1 - 1 / L + mmr)                 # 격리 롱 추정 청산가
        return (price - liq) / price >= (price - sl) / price + self.r["liq_buffer_pct"] / 100 - 1e-12

    def mmr_of(self, mmr):
        return max(mmr or 0, self.r["min_mmr_pct"] / 100)

    def size(self, strategy, price, sl, inst, n_legs=1):
        """목표 위험으로 다리당 수량. (다리당 수량, 이유|None). 최소 주문 때문에 올리면 위험 재확인."""
        B = self.budgets.get(strategy, 0)
        if B <= 0:
            return 0, "예산 없음"
        target = B * self.r["per_trade_risk_pct"] / 100
        per_unit = self.loss_at_sl(1, price, sl)
        total = target / per_unit
        q = floor_step(total / n_legs, inst["qty_step"])
        need = max(inst["min_qty"], ceil_step(inst["min_notional"] / price, inst["qty_step"]))
        if q < need:
            q = need
            risk = self.loss_at_sl(q * n_legs, price, sl) / B * 100
            if risk > self.r["max_risk_after_rounding_pct"]:
                return 0, f"최소 주문 때문에 위험 {risk:.2f}% > {self.r['max_risk_after_rounding_pct']}%"
        return q, None

    # ---------- 진입 검사 ----------
    def check_entry(self, it: EntryIntent, st: RiskState, inst: dict | None, mmr: float | None) -> Decision:
        r = self.r
        if it.mode != self.mode:
            return Decision(False, "모드 불일치")
        if st.halted_global:
            return Decision(False, f"정지 중: {st.halted_global}")
        if st.entries_blocked:
            return Decision(False, f"진입 막힘: {st.entries_blocked}")
        if st.halted_strategy.get(it.strategy):
            return Decision(False, f"전략 정지: {st.halted_strategy[it.strategy]}")
        B = self.budgets.get(it.strategy, 0)
        if B <= 0:
            return Decision(False, "예산 없음")
        # 일일 손실 — 투자 방법(전략)마다 따로 (사용자 결정 10/13: 다른 방법은 영향 없음)
        today = st.realized_today.get(it.strategy, 0.0)
        if today <= -B * r["daily_loss_limit_pct"] / 100:
            return Decision(False, f"일일 손실 한도 ({today:.2f} USDT)")
        # 누적 손실
        cum = st.realized_cum.get(it.strategy, 0.0)
        if cum <= -B * r["cumulative_stop_pct"] / 100:
            return Decision(False, f"누적 손실 정지 ({cum:.2f} USDT)")
        # 중복·보유
        if (it.event_id, it.strategy) in st.decided:
            return Decision(False, "중복 신호")
        opens = st.open_events.get(it.strategy, {})
        if it.event_id in opens:
            return Decision(False, "중복 신호 (진행 중)")
        if any(v["symbol"] == it.symbol for v in opens.values()):
            return Decision(False, "같은 코인 보유 중")
        if it.symbol in st.foreign_symbols:
            return Decision(False, "봇 밖 포지션이 있는 코인")
        if len(opens) >= r["max_positions_per_strategy"]:
            return Decision(False, "동시 포지션 한도")
        # 종목 규칙
        if not inst or inst.get("status") != "Trading":
            return Decision(False, "거래 불가 종목")
        if not it.legs or any(l.qty <= 0 for l in it.legs):
            return Decision(False, "수량 0")
        for l in it.legs:
            if l.qty < inst["min_qty"] - 1e-12 or l.qty * it.price < inst["min_notional"] - 1e-9:
                return Decision(False, "최소 주문 미달")
            if abs(l.qty / inst["qty_step"] - round(l.qty / inst["qty_step"])) > 1e-6:
                return Decision(False, "수량 단위 불일치")
            if l.tp is not None and l.tp <= it.price:
                return Decision(False, "익절가가 진입가 이하")
        # 손절·위험
        if not it.sl or it.sl <= 0 or it.sl >= it.price:
            return Decision(False, "손절 없음/잘못됨")
        if (it.price - it.sl) / it.price > 0.5:
            return Decision(False, "손절이 50% 넘게 멂")
        risk = self.loss_at_sl(it.qty, it.price, it.sl)
        risk_pct = risk / B * 100
        if risk_pct > r["per_trade_risk_cap_pct"] + 1e-9:
            return Decision(False, f"위험 {risk_pct:.2f}% > 상한 {r['per_trade_risk_cap_pct']}%")
        # 레버리지·청산가
        m = self.mmr_of(mmr)
        L = it.leverage or self.choose_leverage(it.price, it.sl, m, inst.get("max_lev", 1))
        if not L or not self.liq_ok(it.price, it.sl, L, m):
            return Decision(False, "청산가가 손절가 안쪽 (레버리지 불가)")
        margin = it.qty * it.price / L
        used = sum(v.get("margin", 0) for v in opens.values())
        if used + margin > B + 1e-9:
            return Decision(False, f"증거금 부족 (사용 {used:.2f} + {margin:.2f} > 예산 {B:.2f})")
        return Decision(True, "ok", L, margin, risk, risk_pct)

    def check_reduce(self, qty, leg_open_qty) -> Decision:
        if qty <= 0:
            return Decision(False, "수량 0")
        if qty > leg_open_qty + 1e-12:
            return Decision(False, "보유보다 큰 감소 주문")
        return Decision(True, "ok")

    # ---------- 정지 판정 ----------
    def new_halts(self, st: RiskState) -> dict:
        """누적 손실 정지가 새로 걸려야 하는 전략 {strategy: 이유}"""
        out = {}
        for s, B in self.budgets.items():
            if B > 0 and not st.halted_strategy.get(s):
                cum = st.realized_cum.get(s, 0.0)
                if cum <= -B * self.r["cumulative_stop_pct"] / 100:
                    out[s] = f"누적 손실 {cum:.2f} USDT ≤ −{self.r['cumulative_stop_pct']}%"
        return out

    def daily_hit(self, st: RiskState) -> list:
        return [s for s, B in self.budgets.items()
                if B > 0 and st.realized_today.get(s, 0.0) <= -B * self.r["daily_loss_limit_pct"] / 100]
