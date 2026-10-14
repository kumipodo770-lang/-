"""
엔진: 신호 → (레짐 관문) → 크기 → 리스크 검사 → 주문 → 체결·손절·익절 반영 → 시간 청산·손절 이동 → 기록·알림.
- 한 스레드에서만 거래소를 만진다. 웹·텔레그램 명령은 큐로 들어와 이 스레드가 처리한다.
- 재시작하면 DB(다리 기록) + 거래소 실제 주문·포지션·체결로 상태를 맞춘다.
- 같은 사건·전략은 decisions 표 + orderLinkId(같은 값이면 거래소가 거부)로 두 번 주문되지 않는다.
- 거래소 호출이 재시도 후에도 실패하면 알림 + 그 장부 신규 진입 중단 (재개는 사용자가 /재개).
"""
import os
import json
import math
import queue
import logging
import threading
import traceback
from . import strategy as S
from .risk import RiskManager, EntryIntent, LegPlan, RiskState, floor_step, ceil_step
from .exchange_base import ExchangeError
from .timeutil import now_ms, kst_day, kst_str, in_capture, MIN, H1

log = logging.getLogger("engine")


class Book:
    def __init__(self, cfg_book, ex, risk_cfg, enabled=True, disabled_reason=""):
        self.name = cfg_book["name"]
        self.mode = cfg_book["mode"]
        self.gate = cfg_book.get("regime_gate") or {}
        self.ex = ex
        self.raw_budgets = {}
        self.risk = RiskManager(risk_cfg, {}, self.mode)
        self.set_budgets(cfg_book.get("budgets"))
        self.enabled = enabled
        self.disabled_reason = disabled_reason
        self.foreign = set()
        self.last_equity = None
        self.last_margin_check = 0
        self.notified = set()

    def set_budgets(self, budgets):
        """예산 칸 적용 (최소 금액 미만은 그 방법만 끔). 바뀐 것 [(전략, 옛값, 새값)] 반환."""
        raw = {k: float(v or 0) for k, v in (budgets or {}).items()}
        eff, notes = {}, {}
        for s in S.SITE_KEY:
            v = raw.get(s, 0.0)
            if 0 < v < S.MIN_BUDGET[s]:
                notes[s] = f"예산 {v:g} < 최소 {S.MIN_BUDGET[s]} USDT → 꺼짐"
                v = 0.0
            if s in raw:
                eff[s] = v
        changed = [(s, self.raw_budgets.get(s, 0.0), raw.get(s, 0.0)) for s in S.SITE_KEY
                   if self.raw_budgets.get(s, 0.0) != raw.get(s, 0.0)]
        self.raw_budgets, self.budgets, self.notes = raw, eff, notes
        self.risk.budgets = dict(eff)
        return changed


class Engine:
    def __init__(self, cfg, store, market, screener, regime, notifier=None, books=None, clock=now_ms, cfg_path=None):
        self.cfg = cfg
        self.cfg_path = cfg_path
        self.cfg_mtime = self._mtime()
        self.last_cfg_check = 0
        self.st = store
        self.m = market
        self.scr = screener
        self.reg = regime
        self.tg = notifier
        self.books: list[Book] = books or []
        self.clock = clock
        self.cmdq = queue.Queue()
        self.last_poll = 0
        self.last_site = 0
        self.last_error = None
        self.mmr_cache = {}
        self.scfg = cfg["strategy"]
        self.lock = threading.RLock()
        self._hi_next = {}
        self._err_seen = {}
        self._stop = False

    # ================= 도우미 =================
    def k(self, book, key):
        return f"{book}:{key}"

    def notify(self, text):
        log.info("알림: %s", text)
        if self.tg:
            self.tg.send(text)

    def err(self, book, kind, msg, block=False):
        key = (book.name if book else "", kind, msg[:80])
        if self.clock() - self._err_seen.get(key, 0) > 10 * MIN:      # 같은 오류는 10분에 한 번만 기록
            self._err_seen[key] = self.clock()
            self.st.event("ERROR", book.name if book else "", kind, msg)
        self.last_error = f"{kst_str(self.clock())} {kind}: {msg}"
        if block and book and not self.st.get(self.k(book.name, "blocked")):
            self.st.put(self.k(book.name, "blocked"), f"{kind}: {msg}"[:200])
            self.notify(f"⚠️ [{book.name}] {kind} — 신규 진입 중단\n{msg[:300]}\n확인 후 /재개 {book.name}")
        elif block is False:
            pass

    def info(self, book, kind, msg):
        self.st.event("INFO", book.name if book else "", kind, msg)

    def mmr(self, symbol):
        d = kst_day(self.clock())
        c = self.mmr_cache.get(symbol)
        if not c or c[0] != d:
            self.mmr_cache[symbol] = (d, self.m.mmr(symbol))
        return self.mmr_cache[symbol][1]

    # ================= 설정 파일 예산 자동 적용 =================
    def _mtime(self):
        try:
            return os.path.getmtime(self.cfg_path) if self.cfg_path else None
        except OSError:
            return None

    def reload_budgets(self):
        """config.yaml 이 바뀌면 예산 칸만 바로 적용 (PC 에서 사용자가 저장한 경우만). 그 밖의 변경은 재시작 필요."""
        from . import config as C
        mt = self._mtime()
        if not mt or mt == self.cfg_mtime:
            return
        self.cfg_mtime = mt
        try:
            new = C.load(self.cfg_path)
        except Exception as e:
            self.notify(f"⚠️ config.yaml 을 읽을 수 없어 이전 설정 유지: {str(e)[:200]}")
            self.st.event("ERROR", "", "설정", str(e)[:200])
            return
        msgs = []
        new_books = {b["name"]: b for b in new["books"]}
        for b in self.books:
            nb = new_books.get(b.name)
            if not nb:
                msgs.append(f"[{b.name}] 설정에서 빠짐 → 재시작해야 적용")
                continue
            ch = b.set_budgets(nb.get("budgets"))
            if ch:
                msgs.append(f"[{b.name}] 예산 " + ", ".join(f"{s} {o:g}→{n:g}" for s, o, n in ch)
                            + "".join(f"\n  {s}: {t}" for s, t in b.notes.items()))
            if nb.get("mode") != b.mode or (nb.get("regime_gate") or {}) != b.gate:
                msgs.append(f"[{b.name}] 모드·레짐 관문 변경은 재시작해야 적용")
        for name in new_books:
            if name not in {b.name for b in self.books}:
                msgs.append(f"[{name}] 새 장부는 재시작해야 켜짐")
        if new["risk"] != self.cfg["risk"] or new["strategy"] != self.cfg["strategy"]:
            msgs.append("한도·전략 값 변경은 재시작해야 적용")
        if msgs:
            self.notify("⚙️ 설정 변경 감지\n" + "\n".join(msgs))
            self.st.event("INFO", "", "설정", " / ".join(msgs)[:500])

    # ================= 리스크 상태 =================
    def risk_state(self, book: Book, event_id=None) -> RiskState:
        n = book.name
        today = kst_day(self.clock())
        base = self.st.get(self.k(n, "cum_base"), {}) or {}
        rt, rc = {}, {}
        for l in self.st.legs(n, ("closed",)):
            if l["pnl_usdt"] is None:
                continue
            s = l["strategy"]
            if l["exit_ts"] and kst_day(l["exit_ts"]) == today:
                rt[s] = rt.get(s, 0.0) + l["pnl_usdt"]
            if l["exit_ts"] and l["exit_ts"] >= base.get(s, 0):
                rc[s] = rc.get(s, 0.0) + l["pnl_usdt"]
        opens = {}
        for l in self.st.active_legs(n):
            ev = opens.setdefault(l["strategy"], {}).setdefault(l["event_id"], {"symbol": l["symbol"], "margin": 0.0})
            ev["margin"] += l["margin"] or 0.0
        decided = set()
        if event_id:
            for s in S.SITE_KEY:
                if self.st.decided(n, event_id, s):
                    decided.add((event_id, s))
        return RiskState(halted_global=self.st.get(self.k(n, "halt")), halted_strategy=self.st.get(self.k(n, "halt_strat"), {}) or {},
                         entries_blocked=self.st.get(self.k(n, "blocked")), realized_today=rt, realized_cum=rc,
                         open_events=opens, decided=decided, foreign_symbols=set(book.foreign))

    # ================= 신호 =================
    def poll_signals(self):
        try:
            data = self.scr.hplan(self.cfg["screener"]["hplan_days"])
        except Exception as e:
            self.err(None, "스크리너", f"{type(e).__name__}: {str(e)[:150]}")
            return
        evs = sorted(data.get("events", []), key=lambda e: e["t_sig"])
        for e in evs:
            if not self.st.has_signal(e["id"]):
                self.st.add_signal(e, self.reg.k1, self.reg.k7)
            if e.get("lim"):
                self.st.update_site(e["id"], e["lim"])
            for b in self.books:
                if not b.enabled:
                    continue
                for s in S.SITE_KEY:
                    if b.budgets.get(s, 0) > 0 and not self.st.decided(b.name, e["id"], s):
                        try:
                            self.handle_entry(b, e, s)
                        except Exception as ex:
                            self.err(b, "진입 처리 오류", f"{e['symbol']} {s}: {ex}", block=True)
                            log.error(traceback.format_exc())

    def handle_entry(self, b: Book, e, s):
        n, sym, eid = b.name, e["symbol"], e["id"]
        sig = self.st.signal(eid) or {}
        k1, k7 = sig.get("k1"), sig.get("k7")

        def skip(reason):
            self.st.decide(n, eid, s, "skip", reason, k1, k7)

        ok, why = S.eligible(e)
        if not ok:
            return skip(why)
        now = self.clock()
        if now >= e["limit_until"]:
            return skip("지정가 유효 시간 지남 (늦게 받음)")
        lim = (e.get("lim") or {}).get("status")
        if lim in ("filled", "expired"):
            return skip(f"사이트 지정가 이미 {lim} (늦게 받음)")
        if b.gate.get("k1_down") == "block":
            if k1 is None:
                return skip("레짐 K1 계산 안 됨 — 막는 설정이라 진입 안 함")
            if k1 == -1:
                return skip("레짐 K1 하락 — 진입 안 함")
        if b.gate.get("low_vol") == "block":
            if k7 is None:
                return skip("레짐 변동성 계산 안 됨 — 막는 설정이라 진입 안 함")
            if k7 == -1:
                return skip("레짐 저변동 — 진입 안 함")
        inst = self.m.instrument(sym)
        if not inst:
            return skip("바이비트 종목 정보 없음")
        tick = inst["tick"]
        price = floor_step(e["limit_px"], tick)
        sl = floor_step(price * (1 - self.scfg["stop_loss_pct"] / 100), tick)
        spec = S.legs_spec(s, self.scfg)
        q, why = b.risk.size(s, price, sl, inst, n_legs=len(spec))
        if not q:
            return skip(why)
        legs = [LegPlan(lg, q, ceil_step(price * (1 + tp / 100), tick) if tp else None) for lg, _, tp in spec]
        it = EntryIntent(n, b.mode, s, eid, sym, price, sl, legs)
        d = b.risk.check_entry(it, self.risk_state(b, eid), inst, self.mmr(sym))
        if not d.ok:
            return skip(d.reason)
        try:
            b.ex.set_leverage(sym, d.leverage)
        except ExchangeError as ex:
            self.err(b, "레버리지 설정 실패", f"{sym} L{d.leverage}: {ex}", block=True)
            return skip(f"레버리지 설정 실패: {ex}")
        self.st.decide(n, eid, s, "enter", f"L{d.leverage} 위험 {d.risk_pct:.2f}% ({d.risk_usdt:.2f} USDT)", k1, k7)
        for lp in legs:
            link = S.link_id(b.mode, s, lp.leg, "E", sym, e["t_sig"])
            lid = self.st.add_leg(book=n, mode=b.mode, event_id=eid, strategy=s, leg=lp.leg, symbol=sym, status="entry_open",
                                  signal_c0=e.get("c0"), limit_px=price, limit_until=e["limit_until"], plan_qty=lp.qty,
                                  lev=d.leverage, margin=lp.qty * price / d.leverage, risk_usdt=d.risk_usdt * lp.qty / it.qty,
                                  entry_link=link, sl_px=sl, tp_px=lp.tp, k1=k1, k7=k7, site_key=S.SITE_KEY[s])
            try:
                oid = b.ex.place_entry(sym, lp.qty, price, link, sl, lp.tp)
                self.st.upd_leg(lid, entry_order_id=oid)
            except ExchangeError as ex:
                o = self._safe(lambda: b.ex.order_by_link(sym, link))
                if o:
                    self.st.upd_leg(lid, entry_order_id=o.order_id)
                    continue
                self.st.upd_leg(lid, status="rejected", reason=str(ex)[:200])
                self.err(b, "진입 주문 거부", f"{sym} {s}{lp.leg}: {ex}", block=True)
        self.info(b, "진입 주문", f"{sym} {s} 지정가 {price} 손절 {sl} L{d.leverage}")

    def _safe(self, fn):
        try:
            return fn()
        except Exception:
            return None

    # ================= 동기화 (거래소 → 기록) =================
    def sync(self, b: Book):
        n = b.name
        now = self.clock()
        msgs = []
        since = self.st.get(self.k(n, "exec_since"), now - 2 * H1)
        try:
            execs = b.ex.executions(max(0, since - 2 * MIN))
            orders = b.ex.open_orders()
            poss = b.ex.positions()
        except ExchangeError as ex:
            self.err(b, "거래소 조회 실패", str(ex), block=True)
            return
        legs = {l["id"]: l for l in self.st.active_legs(n)}
        by_link = {l["entry_link"]: l for l in legs.values()}
        newest = since
        for e in execs:
            newest = max(newest, e.time)
            if self.st.has_exec(n, e.exec_id):
                continue
            leg = self._match_exec(e, legs, by_link)
            if e.exec_type == "Funding":
                self._funding(n, e, legs)
                self.st.add_exec(n, e, None)
                continue
            self.st.add_exec(n, e, leg["id"] if leg else None)
            if not leg:
                if S.is_bot_link(e.link):
                    self.err(b, "짝 없는 체결", f"{e.symbol} {e.link} {e.side} {e.qty}")
                continue
            m = self._apply_exec(b, leg, e)
            if m:
                msgs.append(m)
            legs[leg["id"]] = self.st.leg(leg["id"])
        self.st.put(self.k(n, "exec_since"), newest)
        # 진입 주문 상태
        open_ids = {o.order_id for o in orders}
        for l in self.st.legs(n, ("entry_open",)):
            if l["entry_order_id"] in open_ids:
                if now >= l["limit_until"]:
                    try:     # 취소만 하고, 다음 동기화에서 체결분까지 반영한 뒤 마무리
                        b.ex.cancel(l["symbol"], order_id=l["entry_order_id"])
                        self.st.upd_leg(l["id"], reason="지정가 만료")
                    except ExchangeError:
                        pass
                continue
            o = self._safe(lambda: b.ex.order_by_link(l["symbol"], l["entry_link"]))
            if o and o.status in ("New", "PartiallyFilled"):
                continue
            self._entry_finished(b, self.st.leg(l["id"]), self.st.leg(l["id"])["reason"] or f"주문 {o.status if o else '없음'}")
        # 손절·익절 주문 찾기 / 없으면 다시 걸기
        for l in self.st.legs(n, ("open",)):
            self._protect(b, l, orders)
        # 포지션 대조
        mine = {}
        for l in self.st.legs(n, ("open", "closing", "entry_open")):
            mine[l["symbol"]] = mine.get(l["symbol"], 0.0) + (l["fill_qty"] or 0) - (l["exit_qty"] or 0)
        foreign = set()
        for sym, p in poss.items():
            ours = mine.get(sym, 0.0)
            if p.size < 0 or p.size > ours * 1.001 + 1e-9:
                foreign.add(sym)
            elif p.size < ours * 0.999 - 1e-9:
                key = f"short:{sym}:{round(ours, 8)}:{round(p.size, 8)}"
                if key not in b.notified:
                    b.notified.add(key)
                    self.err(b, "포지션 불일치", f"{sym} 거래소 {p.size} < 기록 {ours:.6g} (청산·ADL·수동 청산?)")
        for sym, ours in mine.items():
            if ours > 1e-9 and sym not in poss:
                key = f"gone:{sym}:{round(ours, 8)}"
                if key not in b.notified and self.st.legs(n, ("open",), "symbol=? AND fill_ts < ?", (sym, now - 2 * MIN)):
                    b.notified.add(key)
                    self.err(b, "포지션 불일치", f"{sym} 거래소에 포지션 없음, 기록은 {ours:.6g}")
        b.foreign = foreign
        if msgs:
            self.notify("\n".join(msgs))

    def _match_exec(self, e, legs, by_link):
        if e.link and e.link in by_link:
            return by_link[e.link]
        for l in legs.values():
            if e.order_id and e.order_id in (l["sl_order_id"], l["tp_order_id"]):
                return l
            ex = json.loads(l["extra"] or "{}")
            if e.link and e.link in ex.get("links", []):
                return l
            if e.order_id and e.order_id in ex.get("order_ids", []):
                return l
        # 거래소가 만든 부분 손절·익절 체결인데 아직 주문 id 를 못 찾은 경우 → 같은 코인, 수량이 맞는 다리
        if e.side == "Sell" and e.stop_type in ("PartialStopLoss", "PartialTakeProfit", "StopLoss", "TakeProfit"):
            cands = [l for l in legs.values() if l["symbol"] == e.symbol and l["status"] in ("entry_open", "open", "closing")
                     and (l["fill_qty"] or 0) > 0 and abs(((l["fill_qty"] or 0) - (l["exit_qty"] or 0)) - e.qty) < 1e-9]
            if e.stop_type.endswith("TakeProfit"):
                cands = [l for l in cands if l["tp_px"]] or cands
            if cands:
                return cands[0]
        return None

    def _funding(self, n, e, legs):
        ls = [l for l in legs.values() if l["symbol"] == e.symbol and l["status"] in ("open", "closing")]
        tot = sum((l["fill_qty"] or 0) - (l["exit_qty"] or 0) for l in ls)
        for l in ls:
            share = ((l["fill_qty"] or 0) - (l["exit_qty"] or 0)) / tot if tot > 0 else 0
            self.st.upd_leg(l["id"], funding=(l["funding"] or 0) + e.fee * share)

    def _apply_exec(self, b, leg, e):
        if e.side == "Buy" and e.link == leg["entry_link"]:
            fq = (leg["fill_qty"] or 0) + e.qty
            fp = ((leg["fill_px"] or 0) * (leg["fill_qty"] or 0) + e.price * e.qty) / fq
            first = not leg["fill_ts"]
            upd = dict(fill_qty=fq, fill_px=fp, entry_fee=(leg["entry_fee"] or 0) + e.fee, entry_maker=int(e.is_maker),
                       max_px=max(leg["max_px"] or 0, fp))
            if first:
                upd.update(fill_ts=e.time, exit_due_ts=e.time + S.hold_ms(leg["strategy"], self.scfg))
                if leg["status"] == "entry_open":
                    upd["status"] = "entry_open"   # 지정가가 다 찰 때까지 entry_open 유지, 아래 _entry_finished 에서 open
            self.st.upd_leg(leg["id"], **upd)
            if first:
                slip = (fp / leg["limit_px"] - 1) * 1e4
                return (f"✅ [{b.name}] {leg['strategy']}{leg['leg']} {leg['symbol']} 체결 {fp:.6g} × {fq:g} "
                        f"(지정가 대비 {slip:+.1f}bp, L{leg['lev']}) 손절 {leg['sl_px']:.6g}" + (f" 익절 {leg['tp_px']:.6g}" if leg["tp_px"] else ""))
            return None
        if e.side == "Sell":
            xq = (leg["exit_qty"] or 0) + e.qty
            upd = dict(exit_qty=xq, exit_value=(leg["exit_value"] or 0) + e.qty * e.price, exit_fee=(leg["exit_fee"] or 0) + e.fee)
            reason = leg["exit_reason"]
            if not reason or reason == "closing":
                if e.stop_type in ("PartialTakeProfit", "TakeProfit"):
                    reason = "TP"
                elif e.stop_type in ("PartialStopLoss", "StopLoss", "Stop"):
                    reason = "SL+" if (leg["armed"] or leg["tp_done"]) else "SL"
                else:
                    reason = reason or "MARKET"
            upd["exit_reason"] = reason
            self.st.upd_leg(leg["id"], **upd)
            leg = self.st.leg(leg["id"])
            if leg["fill_qty"] and xq >= leg["fill_qty"] * 0.999 and leg["status"] != "entry_open":
                return self._close_leg(b, leg, e.time)
        return None

    def _close_leg(self, b, leg, t):
        q = leg["fill_qty"]
        ex_px = leg["exit_value"] / leg["exit_qty"] if leg["exit_qty"] else 0
        pnl = leg["exit_value"] - leg["fill_px"] * q - (leg["entry_fee"] or 0) - (leg["exit_fee"] or 0) - (leg["funding"] or 0)
        pct = pnl / (leg["fill_px"] * q) * 100
        hold = (t - leg["fill_ts"]) / H1 if leg["fill_ts"] else None
        self.st.upd_leg(leg["id"], status="closed", exit_ts=t, exit_px=ex_px, pnl_usdt=pnl, pnl_pct=pct, hold_h=hold)
        # 남은 보호 주문 취소 (한 코인 = 한 포지션이라 남겨 두면 다른 다리를 줄일 수 있음)
        for oid in (leg["sl_order_id"], leg["tp_order_id"]):
            if oid:
                try:
                    b.ex.cancel(leg["symbol"], order_id=oid)
                except ExchangeError:
                    pass
        self._site_result(leg["id"])
        # H4: a 다리 익절 → b 다리 손절 올리기 표시
        if leg["strategy"] == "H4" and leg["leg"] == "a" and leg["exit_reason"] == "TP":
            for o in self.st.legs(b.name, ("open",), "event_id=? AND strategy='H4' AND leg='b'", (leg["event_id"],)):
                self.st.upd_leg(o["id"], tp_done=1)
        return (f"{'🟢' if pnl >= 0 else '🔴'} [{b.name}] {leg['strategy']}{leg['leg']} {leg['symbol']} 청산 {leg['exit_reason']} "
                f"{pct:+.2f}% ({pnl:+.3f} USDT) 보유 {hold:.1f}h" if hold is not None else "")

    def _entry_finished(self, b, l, why):
        """지정가 주문이 더는 살아 있지 않음 → 체결분 있으면 open, 없으면 cancelled"""
        l = self.st.leg(l["id"])
        if (l["fill_qty"] or 0) > 0:
            if (l["exit_qty"] or 0) >= l["fill_qty"] * 0.999:
                self.st.upd_leg(l["id"], status="open")
                self._close_leg(b, self.st.leg(l["id"]), self.clock())
            else:
                self.st.upd_leg(l["id"], status="open")
        else:
            self.st.upd_leg(l["id"], status="cancelled", reason=why)
            self._site_result(l["id"])

    def _protect(self, b, l, orders):
        """체결된 다리의 손절(·익절) 주문 id 확인. 손절이 없으면 새로 건다."""
        sym = l["symbol"]
        mine = [o for o in orders if o.symbol == sym and o.reduce_only]
        taken = {x for r in self.st.legs(b.name, ("open", "closing")) for x in (r["sl_order_id"], r["tp_order_id"]) if x}
        upd = {}
        if not l["sl_order_id"] or l["sl_order_id"] not in {o.order_id for o in mine}:
            c = [o for o in mine if o.stop_type in ("PartialStopLoss", "StopLoss", "Stop") and o.order_id not in taken
                 and (o.parent_link == l["entry_link"] or (not o.parent_link and abs(o.trigger - l["sl_px"]) <= l["sl_px"] * 1e-4))]
            if c:
                upd["sl_order_id"] = c[0].order_id
            elif l["fill_ts"] and self.clock() - l["fill_ts"] > MIN:
                remain = (l["fill_qty"] or 0) - (l["exit_qty"] or 0)
                if remain > 0:
                    ex = json.loads(l["extra"] or "{}")
                    att = len(ex.get("links", [])) + 1
                    link = S.link_id(b.mode, l["strategy"], l["leg"], "S", sym, l["created_ts"], att)
                    try:
                        oid = b.ex.place_stop(sym, remain, l["sl_px"], link)
                        ex.setdefault("links", []).append(link)
                        ex.setdefault("order_ids", []).append(oid)
                        upd.update(sl_order_id=oid, extra=json.dumps(ex))
                        self.err(b, "손절 재설정", f"{sym} {l['strategy']}{l['leg']} 손절 주문이 없어 새로 걸었음 {l['sl_px']}")
                    except ExchangeError as e:
                        self.err(b, "손절 설정 실패", f"{sym}: {e}", block=True)
        if l["tp_px"] and not l["tp_order_id"]:
            c = [o for o in mine if o.stop_type in ("PartialTakeProfit", "TakeProfit") and o.order_id not in taken
                 and (o.parent_link == l["entry_link"] or (not o.parent_link and abs(o.trigger - l["tp_px"]) <= l["tp_px"] * 1e-4))]
            if c:
                upd["tp_order_id"] = c[0].order_id
        if upd:
            self.st.upd_leg(l["id"], **upd)

    # ================= 관리 (시간 청산·손절 이동) =================
    def manage(self, b: Book):
        now = self.clock()
        lock = self.scfg["lock_profit_pct"] / 100
        # 청산 주문을 냈는데 2분 넘게 다 안 닫힌 다리 → 남은 수량 다시 청산
        for l in self.st.legs(b.name, ("closing",)):
            if now - (l["updated_ts"] or now) > 2 * MIN and (l["fill_qty"] or 0) - (l["exit_qty"] or 0) > 0:
                self.close_leg_market(b, l, l["exit_reason"] or "RETRY")
        for l in self.st.legs(b.name, ("open",)):
            remain = (l["fill_qty"] or 0) - (l["exit_qty"] or 0)
            if remain <= 0:
                continue
            if l["exit_due_ts"] and now >= l["exit_due_ts"]:
                self.close_leg_market(b, l, "TIME")
                continue
            new_sl = None
            if l["strategy"] == "H4" and l["leg"] == "b" and l["tp_done"] and l["sl_px"] < l["fill_px"] * (1 + lock) * 0.9999:
                new_sl = l["fill_px"] * (1 + lock)
            if l["strategy"] == "H5" and not l["armed"]:
                hi = self._max_since(b, l)
                if hi and hi >= l["fill_px"] * (1 + self.scfg["h5_trigger_pct"] / 100):
                    self.st.upd_leg(l["id"], armed=1, max_px=hi)
                    new_sl = l["fill_px"] * (1 + lock)
            if new_sl:
                self.move_stop(b, self.st.leg(l["id"]), new_sl)

    def _max_since(self, b, l):
        """체결 뒤 최고가 (1분봉 고가, 마지막 확인 이후만). 다리마다 1분에 한 번만 조회."""
        nxt = self._hi_next.get(l["id"], 0)
        if self.clock() < nxt:
            return None
        self._hi_next[l["id"]] = self.clock() + MIN
        try:
            last = json.loads(l["extra"] or "{}").get("hi_chk", l["fill_ts"])
            rows = self.m.klines(l["symbol"], "1", start=(last // MIN) * MIN, end=self.clock(), limit=1000)
            first = -(-l["fill_ts"] // MIN) * MIN                 # 체결 다음 봉부터 (체결 전 가격을 보지 않음)
            hi = max([r[2] for r in rows if r[0] >= first and r[0] + MIN <= self.clock()] + [l["max_px"] or 0])
            ex = json.loads(l["extra"] or "{}")
            ex["hi_chk"] = rows[-1][0] if rows else last
            self.st.upd_leg(l["id"], max_px=hi, extra=json.dumps(ex))
            return hi
        except Exception as e:
            log.warning("최고가 조회 실패 %s: %s", l["symbol"], e)
            return None

    def move_stop(self, b, l, new_sl):
        sym = l["symbol"]
        inst = self.m.instrument(sym)
        new_sl = ceil_step(new_sl, inst["tick"])
        try:
            last = b.ex.last_price(sym)
        except Exception:
            last = None
        if last is not None and last <= new_sl:
            self.close_leg_market(b, l, "LOCK")          # 이미 그 아래 → 바로 청산 (사이트도 다음 봉에서 같은 처리)
            return
        try:
            if l["sl_order_id"]:
                b.ex.amend_trigger(sym, l["sl_order_id"], new_sl)
                self.st.upd_leg(l["id"], sl_px=new_sl)
                self.info(b, "손절 이동", f"{sym} {l['strategy']}{l['leg']} → {new_sl}")
                return
        except ExchangeError as e:
            log.warning("amend 실패, 새 손절로 교체: %s", e)
        remain = (l["fill_qty"] or 0) - (l["exit_qty"] or 0)
        ex = json.loads(l["extra"] or "{}")
        link = S.link_id(b.mode, l["strategy"], l["leg"], "S", sym, l["created_ts"], len(ex.get("links", [])) + 1)
        try:
            oid = b.ex.place_stop(sym, remain, new_sl, link)
        except ExchangeError as e:
            self.err(b, "손절 이동 실패", f"{sym}: {e}", block=True)
            return
        old = l["sl_order_id"]
        ex.setdefault("links", []).append(link)
        ex.setdefault("order_ids", []).append(oid)
        self.st.upd_leg(l["id"], sl_px=new_sl, sl_order_id=oid, extra=json.dumps(ex))
        if old:
            try:
                b.ex.cancel(sym, order_id=old)
            except ExchangeError:
                pass

    def close_leg_market(self, b, l, reason):
        remain = (l["fill_qty"] or 0) - (l["exit_qty"] or 0)
        d = b.risk.check_reduce(remain, remain)
        if not d.ok:
            return
        ex = json.loads(l["extra"] or "{}")
        att = len(ex.get("links", [])) + 1
        link = S.link_id(b.mode, l["strategy"], l["leg"], "X", l["symbol"], l["created_ts"], att)
        try:
            oid = b.ex.close_market(l["symbol"], remain, link)
        except ExchangeError as e:
            self.err(b, "청산 실패", f"{l['symbol']} {l['strategy']}{l['leg']} {reason}: {e}", block=True)
            return
        ex.setdefault("links", []).append(link)
        ex.setdefault("order_ids", []).append(oid)
        self.st.upd_leg(l["id"], status="closing", exit_reason=reason, extra=json.dumps(ex))

    # ================= 사이트 결과 짝 =================
    def _site_result(self, leg_id):
        l = self.st.leg(leg_id)
        sig = self.st.signal(l["event_id"]) if l else None
        if not sig or not sig.get("site_lim"):
            return
        lim = json.loads(sig["site_lim"])
        v = (lim.get("final") or {}).get(l["site_key"])
        if v is not None:
            self.st.upd_leg(leg_id, site_result=v)

    def sync_site(self):
        try:
            data = self.scr.hplan(self.cfg["screener"]["result_days"])
        except Exception as e:
            self.err(None, "스크리너", str(e)[:150])
            return
        for e in data.get("events", []):
            if self.st.has_signal(e["id"]) and e.get("lim"):
                self.st.update_site(e["id"], e["lim"])
        for l in self.st.q("SELECT id FROM legs WHERE site_result IS NULL AND status IN ('closed','cancelled')"):
            self._site_result(l["id"])

    # ================= 정지·명령 =================
    def check_pending_vs_budget(self, b: Book):
        """아직 체결 안 된 진입 주문이 지금 예산 기준으로 위험 상한을 넘으면 취소 (예산을 줄였거나 그 방법을 끈 경우)"""
        cap = self.cfg["risk"]["per_trade_risk_cap_pct"]
        groups = {}
        for l in self.st.legs(b.name, ("entry_open",)):
            if (l["fill_qty"] or 0) > 0 or l["reason"]:
                continue
            groups.setdefault((l["event_id"], l["strategy"]), []).append(l)
        msgs = []
        for (eid, s), ls in groups.items():
            B = b.budgets.get(s, 0.0)
            risk = sum(x["risk_usdt"] or 0 for x in ls)
            if B > 0 and risk / B * 100 <= cap + 1e-9:
                continue
            why = "그 방법 꺼짐" if B <= 0 else f"예산 {B:g} 기준 위험 {risk / B * 100:.1f}% > 상한 {cap}%"
            for x in ls:
                try:
                    b.ex.cancel(x["symbol"], order_id=x["entry_order_id"])
                except ExchangeError:
                    pass
                self.st.upd_leg(x["id"], reason=f"{why} → 미체결 주문 취소")
            msgs.append(f"{ls[0]['symbol']} {s}: {why}")
        if msgs:
            self.notify(f"🧹 [{b.name}] 예산에 안 맞는 대기 주문 취소\n" + "\n".join(msgs[:20]))
            self.st.event("WARN", b.name, "대기 주문 취소", " / ".join(msgs)[:500])

    def check_limits(self, b: Book):
        self.check_pending_vs_budget(b)
        st = self.risk_state(b)
        hs = st.halted_strategy
        for s, why in b.risk.new_halts(st).items():
            hs[s] = why
            self.st.put(self.k(b.name, "halt_strat"), hs)
            self.notify(f"⛔ [{b.name}] {s} 누적 손실 정지: {why}\n재개는 config.yaml 의 cumulative_reset_id 를 올리고 재시작해야 합니다.")
            self.st.event("WARN", b.name, "누적 정지", f"{s} {why}")
        today = kst_day(self.clock())
        for s in b.risk.daily_hit(st):
            key = f"daily:{today}:{s}"
            if key not in b.notified:
                b.notified.add(key)
                self.notify(f"⏸ [{b.name}] {s} 일일 손실 한도 도달 — 오늘(KST) 신규 진입 중단")
                self.st.event("WARN", b.name, "일일 한도", s)
        # 누적 정지 해제 (설정 파일 숫자를 올린 경우만)
        rid = int(self.cfg["risk"].get("cumulative_reset_id", 0))
        if rid > int(self.st.get(self.k(b.name, "reset_id"), 0) or 0):
            self.st.put(self.k(b.name, "reset_id"), rid)
            self.st.put(self.k(b.name, "cum_base"), {s: self.clock() for s in S.SITE_KEY})
            self.st.put(self.k(b.name, "halt_strat"), {})
            self.notify(f"[{b.name}] 누적 손실 기준을 지금으로 다시 잡고 전략 정지를 풀었습니다 (설정 cumulative_reset_id={rid}).")
        # 격리 마진 확인 (demo/live)
        if b.mode != "paper" and self.clock() - b.last_margin_check > 10 * MIN:
            b.last_margin_check = self.clock()
            try:
                mm = b.ex.margin_mode()
                if mm != "ISOLATED_MARGIN":
                    self.err(b, "마진 모드", f"계정 마진 모드가 {mm} — 격리(ISOLATED_MARGIN)로 직접 바꿔 주세요", block=True)
                b.last_equity = b.ex.equity()          # 웹 예산 올리기 검사용 (예산 합계 ≤ 계좌 USDT)
            except ExchangeError as e:
                self.err(b, "계정 조회 실패", str(e), block=True)

    # ================= 웹에서 예산 수정 (사용자 결정 2026-10-13) =================
    def check_budget_change(self, book, budgets):
        """웹 확인 단계 (읽기만). (ok, 메시지, 바뀌는 것 [(전략, 옛값, 새값)], 올리는 게 있는지)"""
        b = next((x for x in self.books if x.name == book), None)
        if not b:
            return False, f"장부 {book} 없음", [], False
        if not b.enabled:
            return False, f"꺼진 장부: {b.disabled_reason}", [], False
        new = dict(b.raw_budgets)
        for s, v in (budgets or {}).items():
            if s not in S.SITE_KEY:
                return False, f"알 수 없는 종류 {s}", [], False
            try:
                v = float(v)
            except (TypeError, ValueError):
                return False, f"{s}: 숫자가 아님", [], False
            if v < 0 or v > 1_000_000 or math.isnan(v):
                return False, f"{s}: 0 ~ 1,000,000 사이로", [], False
            if 0 < v < S.MIN_BUDGET[s]:
                return False, f"{s}: 최소 {S.MIN_BUDGET[s]} USDT (0 = 끄기)", [], False
            new[s] = round(v, 2)
        diff = [(s, b.raw_budgets.get(s, 0.0), new[s]) for s in S.SITE_KEY if new.get(s, 0.0) != b.raw_budgets.get(s, 0.0)]
        if not diff:
            return False, "바뀐 값이 없습니다", [], False
        up = any(n > o for _, o, n in diff)
        if b.mode != "paper" and up:
            eq = getattr(b, "last_equity", None)
            if eq is None:
                return False, "거래소 잔고를 아직 못 읽어서 올릴 수 없습니다 (잠시 뒤 다시)", diff, up
            if sum(new.values()) > eq:
                return False, f"예산 합계 {sum(new.values()):g} > 계좌 USDT {eq:.2f}", diff, up
        return True, "ok", diff, up

    def request_budgets(self, book, budgets):
        """확인·비밀번호 통과 뒤 웹이 호출. 실제 적용은 엔진 스레드에서."""
        self.cmdq.put(("budget", book, dict(budgets)))
        return "접수: 예산 변경 — 적용되면 텔레그램으로 알려 드립니다."

    def _apply_budgets(self, book, budgets):
        from . import config as C
        ok, msg, diff, _ = self.check_budget_change(book, budgets)
        if not ok:
            self.notify(f"⚠️ 예산 변경 거부: {msg}")
            return
        b = next(x for x in self.books if x.name == book)
        new = dict(b.raw_budgets)
        new.update({s: n for s, _, n in diff})
        if self.cfg_path:
            try:
                C.write_budgets(self.cfg_path, book, {s: new.get(s, 0) for s in S.SITE_KEY if s in new},
                                backup_dir=os.path.join(os.path.dirname(self.cfg_path), "data", "config_backup"))
                self.cfg_mtime = self._mtime()
            except Exception as e:
                self.notify(f"⚠️ 예산 저장 실패 (적용 안 함): {str(e)[:200]}")
                self.st.event("ERROR", book, "예산 저장", str(e)[:200])
                return
        b.set_budgets(new)
        txt = ", ".join(f"{s} {o:g}→{n:g}" for s, o, n in diff)
        self.notify(f"💰 [{book}] 예산 변경 (웹): {txt}" + "".join(f"\n  {s}: {t}" for s, t in b.notes.items()))
        self.st.event("WARN", book, "예산 변경(웹)", txt)
        self.check_pending_vs_budget(b)

    def command(self, cmd, book="all"):
        """웹·텔레그램에서 호출 (다른 스레드). status 는 바로, 나머지는 엔진 스레드가 처리."""
        if cmd == "status":
            return self.status_text(book)
        if cmd not in ("stop", "resume", "closeall"):
            return "알 수 없는 명령"
        names = [b.name for b in self.books]
        if book != "all" and book not in names:
            return f"장부 이름이 없습니다: {book} (있는 것: {', '.join(names)})"
        self.cmdq.put((cmd, book))
        return f"접수: {cmd} [{book}] — 처리되면 알림이 옵니다."

    def _do_command(self, cmd, book):
        for b in self.books:
            if book != "all" and b.name != book:
                continue
            if cmd in ("stop", "closeall"):
                self.st.put(self.k(b.name, "halt"), "수동 정지" if cmd == "stop" else "전체 청산")
                for l in self.st.legs(b.name, ("entry_open",)):
                    try:
                        b.ex.cancel(l["symbol"], order_id=l["entry_order_id"])
                        self.st.upd_leg(l["id"], reason="수동 정지로 취소")
                    except ExchangeError:
                        pass
                b.ex.tick(self.clock())
                self.sync(b)                       # 취소 직전 체결분까지 반영
                if cmd == "closeall":
                    for l in self.st.legs(b.name, ("open",)):
                        self.close_leg_market(b, l, "CLOSEALL")
                    b.ex.tick(self.clock())
                    self.sync(b)
                self.notify(f"🛑 [{b.name}] {'정지' if cmd == 'stop' else '전체 청산'} 완료 — 신규 진입 중단")
                self.st.event("WARN", b.name, "명령", cmd)
            elif cmd == "resume":
                self.st.put(self.k(b.name, "halt"), None)
                self.st.put(self.k(b.name, "blocked"), None)
                hs = self.st.get(self.k(b.name, "halt_strat"), {}) or {}
                extra = f" (누적 정지 전략 {', '.join(hs)} 은 그대로 — 설정 파일로만 해제)" if hs else ""
                self.notify(f"▶️ [{b.name}] 재개{extra}")
                self.st.event("INFO", b.name, "명령", "resume")

    # ================= 상태 =================
    def snapshot(self):
        """화면·텔레그램용 상태. 방법(종류)마다: 예산, 지금 들어가 있는 금액(증거금·포지션), 대기 주문, 이익·손실·순손익, 보유 평가손익 + 세 종류 합계"""
        now = self.clock()
        out = {"now": now, "regime": self.reg.snapshot() if self.reg else {}, "last_error": self.last_error, "books": []}
        try:
            prices = self.m.all_tickers()
        except Exception:
            prices = {}
        for b in self.books:
            st = self.risk_state(b)
            closed = self.st.legs(b.name, ("closed",))
            active = self.st.active_legs(b.name)
            per = {}
            keys = ("budget", "margin", "notional", "pending_margin", "profit", "loss", "net", "upnl", "today", "fees")
            tot = {k: 0.0 for k in keys} | {"n_open": 0, "n_pending": 0, "n_win": 0, "n_loss": 0, "n_closed": 0, "upnl_known": True}
            for s in S.SITE_KEY:
                if s not in b.budgets and s not in b.notes:
                    continue
                B = b.budgets.get(s, 0.0)
                cl = [l for l in closed if l["strategy"] == s]
                ac = [l for l in active if l["strategy"] == s]
                margin = notional = pend = upnl = 0.0
                n_open_ev, n_pend_ev, upnl_known = set(), set(), True
                for l in ac:
                    rem = (l["fill_qty"] or 0) - (l["exit_qty"] or 0)
                    if rem > 0:
                        n_open_ev.add(l["event_id"])
                        notional += rem * l["fill_px"]
                        margin += rem * l["fill_px"] / (l["lev"] or 1)
                        px = prices.get(l["symbol"])
                        if px:
                            upnl += rem * (px - l["fill_px"])
                        else:
                            upnl_known = False
                    if l["status"] == "entry_open" and (l["plan_qty"] or 0) > (l["fill_qty"] or 0):
                        n_pend_ev.add(l["event_id"])
                        pend += ((l["plan_qty"] or 0) - (l["fill_qty"] or 0)) * l["limit_px"] / (l["lev"] or 1)
                pnls = [l["pnl_usdt"] or 0.0 for l in cl]
                row = {"rank": S.INFO[s][0], "desc": S.INFO[s][1], "budget": B, "min_budget": S.MIN_BUDGET.get(s),
                       "raw_budget": b.raw_budgets.get(s, 0.0),
                       "note": b.notes.get(s), "halt": st.halted_strategy.get(s),
                       "margin": round(margin, 4), "notional": round(notional, 4), "pending_margin": round(pend, 4),
                       "n_open": len(n_open_ev), "n_pending": len(n_pend_ev),
                       "profit": round(sum(p for p in pnls if p > 0), 4), "loss": round(sum(p for p in pnls if p < 0), 4),
                       "net": round(sum(pnls), 4), "n_win": sum(p > 0 for p in pnls), "n_loss": sum(p <= 0 for p in pnls),
                       "n_closed": len(cl), "upnl": round(upnl, 4), "upnl_known": upnl_known,
                       "today": round(st.realized_today.get(s, 0.0), 4),
                       "fees": round(sum((l["entry_fee"] or 0) + (l["exit_fee"] or 0) + (l["funding"] or 0) for l in cl), 4),
                       # 이전 화면과의 호환
                       "cum": round(sum(pnls), 4), "open": len(n_open_ev)}
                per[s] = row
                for k in keys:
                    tot[k] += row[k]
                for k in ("n_open", "n_pending", "n_win", "n_loss", "n_closed"):
                    tot[k] += row[k]
                tot["upnl_known"] = tot["upnl_known"] and upnl_known
            tot = {k: (round(v, 4) if isinstance(v, float) else v) for k, v in tot.items()}
            out["books"].append({"name": b.name, "mode": b.mode, "enabled": b.enabled, "disabled_reason": b.disabled_reason,
                                 "halt": st.halted_global, "blocked": st.entries_blocked, "gate": b.gate, "strategies": per,
                                 "total": tot, "foreign": sorted(b.foreign), "equity": b.last_equity,
                                 "gate_state": self.gate_state(b) if any(v == "block" for v in b.gate.values()) else None})
        return out

    def gate_state(self, b, k1=None, k7=None):
        """그 장부가 지금 레짐에서 진입하는지 (열림/막힘 + 이유)"""
        k1 = self.reg.k1 if k1 is None and self.reg else k1
        k7 = self.reg.k7 if k7 is None and self.reg else k7
        why = []
        if b.gate.get("k1_down") == "block" and (k1 is None or k1 == -1):
            why.append("하락장" if k1 == -1 else "K1 모름")
        if b.gate.get("low_vol") == "block" and (k7 is None or k7 == -1):
            why.append("저변동" if k7 == -1 else "변동성 모름")
        return ("막힘 (" + "·".join(why) + ")") if why else "열림"

    def check_regime_change(self):
        if not self.reg:
            return
        cur = (self.reg.k1, self.reg.k7)
        prev = getattr(self, "_last_regime", None)
        self._last_regime = cur
        if prev is None or prev == cur or None in cur:
            return
        from .regime import K1_TXT, K7_TXT
        snap = self.reg.snapshot()
        lines = [f"📊 레짐 바뀜: K1 {K1_TXT.get(prev[0])}→{K1_TXT.get(cur[0])} · 변동성 {K7_TXT.get(prev[1])}→{K7_TXT.get(cur[1])}"]
        lines += snap.get("explain", [])
        for b in self.books:
            if b.enabled and any(v == "block" for v in b.gate.values()):
                was, now = self.gate_state(b, *prev), self.gate_state(b, *cur)
                if was != now:
                    lines.append(f"[{b.name}] 진입 {was} → {now}")
        self.notify("\n".join(lines))
        self.st.event("INFO", "", "레짐 변경", " / ".join(lines)[:500])

    def status_text(self, book="all"):
        s = self.snapshot()
        r = s["regime"]
        lines = [f"레짐: K1 {r.get('k1_txt', r.get('k1'))} · 변동성 {r.get('k7_txt', r.get('k7'))}"
                 f" ({r.get('rv_pct') and round(r['rv_pct'])}백분위)"] + list(r.get("explain") or [])
        for b in self.books:
            if b.enabled and any(v == "block" for v in b.gate.values()) and (book == "all" or b.name == book):
                lines.append(f"[{b.name}] 지금 진입: {self.gate_state(b)}")
        for b in s["books"]:
            if book != "all" and b["name"] != book:
                continue
            st = "꺼짐: " + b["disabled_reason"] if not b["enabled"] else ("정지: " + b["halt"] if b["halt"] else ("진입 막힘: " + b["blocked"] if b["blocked"] else "작동 중"))
            lines.append(f"[{b['name']}/{'가상 돈' if b['mode'] == 'paper' else b['mode']}] {st}")
            for k, v in b["strategies"].items():
                if v.get("note"):
                    lines.append(f"{v['rank']} {k}: {v['note']}")
                    continue
                lines.append(f"{v['rank']} {k} (예산 {v['budget']:g}) 들어가 있음 {v['margin']:.2f} (포지션 {v['notional']:.2f}, {v['n_open']}개"
                             + (f", 대기 {v['n_pending']}" if v["n_pending"] else "") + ")"
                             + f"\n   이익 {v['profit']:+.2f} · 손실 {v['loss']:+.2f} · 순 {v['net']:+.2f} · 보유 평가 {v['upnl']:+.2f}"
                             + (f" ⛔{v['halt']}" if v["halt"] else ""))
            t = b.get("total") or {}
            if t:
                lines.append(f"합계 들어가 있음 {t['margin']:.2f} · 총 이익 {t['profit']:+.2f} · 총 손실 {t['loss']:+.2f} · "
                             f"순 {t['net']:+.2f} · 보유 평가 {t['upnl']:+.2f} USDT")
        if s["last_error"]:
            lines.append("마지막 오류: " + s["last_error"][:200])
        return "\n".join(lines)

    # ================= 루프 =================
    def step(self):
        now = self.clock()
        while not self.cmdq.empty():
            item = self.cmdq.get()
            try:
                if item[0] == "budget":
                    self._apply_budgets(item[1], item[2])
                else:
                    self._do_command(item[0], item[1])
            except Exception as e:
                self.err(None, "명령 처리 오류", f"{item[0]}: {e}")
        if self.reg:
            self.reg.refresh()
            self.check_regime_change()
        if now - self.last_cfg_check >= MIN:
            self.last_cfg_check = now
            self.reload_budgets()
        if now - self.last_poll >= self.cfg["screener"]["poll_sec"] * 1000:
            self.last_poll = now
            self.poll_signals()
        for b in self.books:
            if not b.enabled:
                continue
            try:
                b.ex.tick(now)
            except Exception as e:
                self.err(b, "paper 시세 처리 오류", str(e)[:200])
            self.sync(b)
            self.manage(b)
            self.check_limits(b)
        if now - self.last_site >= 10 * MIN and not in_capture(now):
            self.last_site = now
            self.sync_site()
        self.st.put("heartbeat", now)

    def _watchdog(self, limit_sec=90):
        """한 바퀴가 limit_sec 넘게 안 끝나면 텔레그램으로 알림 (멈춤 감시, 한 번 멈출 때 한 번)"""
        import time
        warned = None
        while not self._stop:
            t0 = self._step_t0
            if t0 and time.time() - t0 > limit_sec and warned != t0:
                warned = t0
                msg = f"엔진 한 바퀴가 {limit_sec}초 넘게 안 끝남 — 거래소·스크리너 응답 지연이나 오류일 수 있음. 현황판 '엔진' 표시 확인"
                log.error(msg)
                self.notify("⚠️ " + msg)
            time.sleep(10)

    def run_forever(self, interval=5):
        import time
        self._step_t0 = None
        threading.Thread(target=self._watchdog, daemon=True, name="watchdog").start()
        self.notify("🤖 자동매매 시작\n" + self.status_text())
        while not self._stop:
            t0 = time.time()
            self._step_t0 = t0
            try:
                self.step()
            except Exception as e:
                self.err(None, "루프 오류", f"{type(e).__name__}: {e}")
                log.error(traceback.format_exc())
            self._step_t0 = None
            time.sleep(max(1.0, interval - (time.time() - t0)))
