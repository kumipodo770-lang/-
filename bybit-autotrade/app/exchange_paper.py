"""
paper: 실시간 시세(바이비트 공개 1분봉·현재가) + 가상 체결. 실주문 없음.
체결 규칙 (사이트 리플레이보다 약간 보수적):
- 지정가 매수: 주문 후 처음 시작하는 1분봉부터, 저가가 지정가보다 '낮을 때'(같으면 안 됨) 지정가에 체결 (메이커 수수료).
  주문 순간 현재가가 지정가 이하면 그 자리에서 현재가로 체결 (테이커).
- 진입 주문에 붙인 손절·익절은 체결 수량만큼 '부분 손절/익절' 주문으로 생기고, 체결 다음 봉부터 작동.
- 손절(하락 트리거): 저가 ≤ 트리거 → min(트리거, 시가) × (1 − 슬리피지). 익절(상승 트리거): 고가 ≥ 트리거 → max(트리거, 시가) × (1 − 슬리피지).
  같은 봉에서 둘 다면 손절 먼저. 시장가 청산은 현재가 × (1 − 슬리피지).
- 펀딩: 정산 시각에 포지션이 있으면 실제 펀딩률(공개 이력) × 명목가치. 양수 = 롱이 냄.
상태는 save 콜백으로 저장 → 재시작해도 이어짐.
"""
import json
import math
import logging
import itertools
from .exchange_base import Exchange, Order, Position, Execution, ExchangeError, ACTIVE
from .timeutil import MIN, now_ms

log = logging.getLogger("paper")


def _ceil_min(ms):
    return int(math.ceil(ms / MIN) * MIN)


class PaperExchange(Exchange):
    mode = "paper"

    def __init__(self, market, fee_taker=0.055, fee_maker=0.02, slippage=0.05, start_equity=10000.0,
                 state: dict | None = None, save=None, clock=now_ms):
        self.m = market
        self.ft, self.fm, self.slip = fee_taker / 100, fee_maker / 100, slippage / 100
        self.save_cb = save
        self.clock = clock
        st = state or {}
        self.orders: dict[str, Order] = {k: Order(**v) for k, v in st.get("orders", {}).items()}
        self.attach: dict[str, list] = st.get("attach", {})          # 진입 주문 id → [sl, tp]
        self.pos: dict[str, dict] = st.get("pos", {})                # symbol → {size, avg}
        self.execs: list[Execution] = [Execution(**e) for e in st.get("execs", [])]
        self.cash = st.get("cash", start_equity)                     # 실현 손익·수수료 반영
        self.last_bar: dict[str, int] = st.get("last_bar", {})       # symbol → 마지막으로 처리한 1분봉 시작
        self.fund_done: dict[str, int] = st.get("fund_done", {})     # symbol → 마지막 처리한 펀딩 시각
        self.seq = itertools.count(st.get("seq", 1))
        self.lev: dict[str, int] = st.get("lev", {})

    # ---------- 저장 ----------
    def state(self):
        return {"orders": {k: o.to_dict() for k, o in self.orders.items()}, "attach": self.attach, "pos": self.pos,
                "execs": [e.__dict__ for e in self.execs[-5000:]], "cash": self.cash, "last_bar": self.last_bar,
                "fund_done": self.fund_done, "seq": next(self.seq), "lev": self.lev}

    def _save(self):
        if self.save_cb:
            st = self.state()
            self.seq = itertools.count(st["seq"])
            self.save_cb(json.dumps(st))

    # ---------- 정보 ----------
    def margin_mode(self):
        return "ISOLATED_MARGIN"

    def equity(self):
        up = 0.0
        for s, p in self.pos.items():
            if p["size"] > 0:
                try:
                    up += (self.last_price(s) - p["avg"]) * p["size"]
                except Exception:
                    pass
        return self.cash + up

    def set_leverage(self, symbol, lev):
        self.lev[symbol] = int(lev)

    def last_price(self, symbol):
        return self.m.ticker(symbol)["last"]

    def positions(self):
        out = {}
        for s, p in self.pos.items():
            if p["size"] > 1e-12:
                L = self.lev.get(s, 1)
                out[s] = Position(s, p["size"], p["avg"], L, p["avg"] * (1 - 1 / L + 0.01) if L > 1 else 0.0)
        return out

    def open_orders(self):
        return [o for o in self.orders.values() if o.status in ACTIVE]

    def order_by_link(self, symbol, link):
        for o in self.orders.values():
            if o.link == link and o.symbol == symbol:
                return o
        return None

    def executions(self, since_ms):
        return [e for e in self.execs if e.time >= since_ms]

    # ---------- 주문 ----------
    def _new_id(self):
        return f"P{next(self.seq):08d}"

    def _check_link(self, link):
        if link and any(o.link == link for o in self.orders.values()):
            raise ExchangeError("EC_DuplicatedClOrdID")

    def _validate(self, symbol, qty, price=None):
        inst = self.m.instrument(symbol)
        if not inst or inst.get("status") != "Trading":
            raise ExchangeError(f"{symbol} 거래 불가")
        if qty < inst["min_qty"] - 1e-12:
            raise ExchangeError("수량 최소 미달")
        if price is not None and qty * price < inst["min_notional"] - 1e-9:
            raise ExchangeError("최소 주문 금액 미달")

    def place_entry(self, symbol, qty, price, link, sl, tp=None):
        self._check_link(link)
        self._validate(symbol, qty, price)
        now = self.clock()
        o = Order(self._new_id(), link, symbol, "Buy", "Limit", qty, price, 0.0, False, "New", created=now, updated=now)
        self.orders[o.order_id] = o
        self.attach[o.order_id] = [sl, tp]
        last = self.last_price(symbol)
        if last <= price:                       # 바로 체결되는 지정가 = 테이커
            self._fill_entry(o, last, now, maker=False)
        self._save()
        return o.order_id

    def place_stop(self, symbol, qty, trigger, link):
        self._check_link(link)
        now = self.clock()
        o = Order(self._new_id(), link, symbol, "Sell", "Market", qty, 0.0, trigger, True, "Untriggered", stop_type="Stop",
                  created=now, updated=now)
        self.orders[o.order_id] = o
        self._save()
        return o.order_id

    def amend_trigger(self, symbol, order_id, trigger):
        o = self.orders.get(order_id)
        if not o or o.status != "Untriggered":
            raise ExchangeError("EC_OrderNotExist")
        o.trigger, o.updated = trigger, self.clock()
        self._save()

    def cancel(self, symbol, order_id=None, link=None):
        for o in self.orders.values():
            if (order_id and o.order_id == order_id) or (link and o.link == link):
                if o.status in ACTIVE:
                    o.status, o.updated = "Cancelled", self.clock()
                    self._save()
                    return
                raise ExchangeError("EC_OrderNotExist")
        raise ExchangeError("EC_OrderNotExist")

    def close_market(self, symbol, qty, link):
        self._check_link(link)
        p = self.pos.get(symbol, {"size": 0})
        q = min(qty, p["size"])
        if q <= 0:
            raise ExchangeError("포지션 없음 (reduce-only)")
        now = self.clock()
        o = Order(self._new_id(), link, symbol, "Sell", "Market", q, 0.0, 0.0, True, "New", created=now, updated=now)
        self.orders[o.order_id] = o
        px = self.last_price(symbol) * (1 - self.slip)
        self._exec(o, q, px, now, maker=False)
        o.status = "Filled"
        self._save()
        return o.order_id

    # ---------- 체결 내부 ----------
    def _exec(self, o: Order, qty, px, t, maker):
        p = self.pos.setdefault(o.symbol, {"size": 0.0, "avg": 0.0})
        if o.side == "Sell":
            qty = min(qty, p["size"])
        if qty <= 0:
            return
        fee = qty * px * (self.fm if maker else self.ft)
        if o.side == "Buy":
            if p["size"] <= 0:
                self.fund_done[o.symbol] = t          # 이 시각 이후 정산부터 펀딩
            new = p["size"] + qty
            p["avg"] = (p["avg"] * p["size"] + px * qty) / new
            p["size"] = new
        else:
            self.cash += (px - p["avg"]) * qty
            p["size"] -= qty
            if p["size"] <= 1e-12:
                p["size"], p["avg"] = 0.0, 0.0
                for x in self.orders.values():      # 포지션이 0 이 되면 남은 감소 전용 조건 주문은 무효
                    if x.symbol == o.symbol and x.reduce_only and x.status == "Untriggered":
                        x.status, x.updated = "Deactivated", t
        self.cash -= fee
        o.cum_qty += qty
        o.avg_price = px if o.cum_qty == qty else (o.avg_price * (o.cum_qty - qty) + px * qty) / o.cum_qty
        o.updated = t
        self.execs.append(Execution(f"E{next(self.seq):09d}", o.order_id, o.link, o.symbol, o.side, qty, px, fee, "Trade", t,
                                    maker, o.stop_type))

    def _fill_entry(self, o, px, t, maker):
        self._exec(o, o.qty, px, t, maker)
        o.status = "Filled"
        sl, tp = self.attach.get(o.order_id, [None, None])
        if sl:
            i = self._new_id()
            self.orders[i] = Order(i, "", o.symbol, "Sell", "Market", o.qty, 0.0, sl, True, "Untriggered",
                                   stop_type="PartialStopLoss", parent_link=o.link, created=t, updated=t)
        if tp:
            i = self._new_id()
            self.orders[i] = Order(i, "", o.symbol, "Sell", "Market", o.qty, 0.0, tp, True, "Untriggered",
                                   stop_type="PartialTakeProfit", parent_link=o.link, created=t, updated=t)

    def tick(self, now_ms_=None):
        now = now_ms_ or self.clock()
        cur_min = (now // MIN) * MIN
        syms = {o.symbol for o in self.orders.values() if o.status in ACTIVE} | {s for s, p in self.pos.items() if p["size"] > 0}
        changed = False
        for s in sorted(syms):
            start = self.last_bar.get(s)
            cand = [_ceil_min(o.created) for o in self.orders.values() if o.symbol == s and o.status in ACTIVE]
            if self.pos.get(s, {}).get("size", 0) > 0:
                cand.append(start + MIN if start is not None else cur_min)
            if not cand:
                continue
            frm = min(cand)
            if start is not None:
                frm = max(frm, start + MIN)
            while frm < cur_min:                       # 봇이 오래 꺼졌으면 1000봉씩 나눠서
                end = min(cur_min, frm + 1000 * MIN)
                bars = [b for b in self.m.klines(s, "1", start=frm, end=end - 1, limit=1000) if frm <= b[0] < end]
                for b in bars:
                    self._bar(s, b)
                    self.last_bar[s] = b[0]
                    changed = True
                if not bars:
                    break
                frm = bars[-1][0] + MIN
            self._funding(s, now)
        if changed:
            self._save()

    def _bar(self, s, b):
        t0, o_, h, l, c = b
        t_end = t0 + MIN
        # 1) 진입 지정가
        for o in [x for x in self.orders.values() if x.symbol == s and x.status == "New" and not x.reduce_only and x.otype == "Limit"]:
            if _ceil_min(o.created) <= t0 and l < o.price:
                self._fill_entry(o, o.price, t_end, maker=True)
        # 2) 손절 먼저, 3) 익절
        stops = [x for x in self.orders.values() if x.symbol == s and x.status == "Untriggered" and _ceil_min(x.created) <= t0]
        for o in [x for x in stops if x.stop_type in ("PartialStopLoss", "StopLoss", "Stop")]:
            if o.status == "Untriggered" and l <= o.trigger:
                self._trigger(o, min(o.trigger, o_) * (1 - self.slip), t_end)
        for o in [x for x in stops if x.stop_type in ("PartialTakeProfit", "TakeProfit")]:
            if o.status == "Untriggered" and h >= o.trigger:
                self._trigger(o, max(o.trigger, o_) * (1 - self.slip), t_end)

    def _trigger(self, o, px, t):
        size = self.pos.get(o.symbol, {}).get("size", 0)
        if size <= 0:
            o.status, o.updated = "Deactivated", t
            return
        self._exec(o, min(o.qty, size), px, t, maker=False)
        o.status = "Filled"

    def _funding(self, s, now):
        p = self.pos.get(s)
        if not p or p["size"] <= 0:
            self.fund_done[s] = now
            return
        inst = self.m.instrument(s) or {}
        iv = int(inst.get("funding_min", 480)) * MIN
        last_done = self.fund_done.get(s, now)
        k = (last_done // iv + 1) * iv
        while k <= now:
            try:
                hist = self.m.funding_history(s, start=k - MIN, end=k + MIN, limit=5)
                rate = next((r for ts, r in hist if abs(ts - k) < MIN), None)
                if rate is not None:
                    px = self.m.klines(s, "1", start=k - MIN, end=k, limit=2)
                    mark = px[-1][4] if px else p["avg"]
                    fee = p["size"] * mark * rate
                    self.cash -= fee
                    self.execs.append(Execution(f"F{next(self.seq):09d}", "", "", s, "", p["size"], mark, fee, "Funding", k))
            except Exception as e:
                log.warning("paper 펀딩 계산 실패 %s: %s", s, e)
                break
            self.fund_done[s] = k
            k += iv
