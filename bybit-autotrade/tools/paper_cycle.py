"""
paper 한 사이클 점검 (실제 시세, 가상 체결, 실주문 없음).
진짜 엔진·paper 거래소·리스크 모듈·기록·알림을 그대로 쓰고, 시험용으로 손절·익절을 아주 좁게(±0.1~0.15%), 보유를 짧게(12~24분) 바꿔
몇십 분 안에 신호 → 지정가 체결 → 손절/익절/시간 청산 → 기록 → 알림 이 끝나는지 본다.
신호는 스크리너 대신 이 스크립트가 만든다 (DOGEUSDT, 지정가 = 현재가 × 0.9999).
결과: data/paper_cycle_<시각>.db, data/paper_cycle_<시각>.txt
"""
import os
import sys
import time
import json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import config as C
from app.store import Store
from app.market import Market
from app.regime import Regime
from app.engine import Engine, Book
from app.exchange_paper import PaperExchange
from app.timeutil import now_ms, kst_str, in_capture

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SYM = sys.argv[1] if len(sys.argv) > 1 else "DOGEUSDT"
MULT = float(sys.argv[2]) if len(sys.argv) > 2 else 0.9999     # 1.0005 = 현재가 위 지정가 → 바로 체결(테이커)
TAG = time.strftime("%m%d_%H%M%S")
OUT = os.path.join(ROOT, "data", f"paper_cycle_{TAG}")


class OneSignal:
    def __init__(self, ev):
        self.ev = ev

    def hplan(self, days=2):
        return {"ready": True, "events": [self.ev]}


class Notes:
    def __init__(self, path, tg=None):
        self.path, self.tg, self.ok, self.chat_id = path, tg, True, "local"

    def send(self, text, buttons=None):
        line = f"[{kst_str(now_ms())}] {text}"
        print("알림 →", line, flush=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        if self.tg:
            self.tg.send("[paper 점검] " + text)


def main():
    if in_capture():
        print("캡처 시간대 — 나중에"); return
    C.load_dotenv()
    cfg = C._merge(C.DEFAULTS, {})
    cfg["strategy"].update({"stop_loss_pct": 0.15, "h4_take_profit_pct": 0.10, "lock_profit_pct": 0.02, "h5_trigger_pct": 0.06,
                            "hold_hours": {"H3": 0.2, "H4": 0.4, "H5": 0.4}})
    cfg["books"] = [{"name": "paper_cycle", "mode": "paper", "budgets": {"H3": 3000, "H4": 3000, "H5": 3000},
                     "regime_gate": {"k1_down": "record", "low_vol": "record"}}]
    cfg["screener"]["poll_sec"] = 5
    C.validate(cfg)
    m = Market()
    store = Store(OUT + ".db")
    reg = Regime(m); reg.refresh(force=True)
    px = m.ticker(SYM)["last"]
    t = now_ms()
    ev = {"id": f"{SYM}-TEST-{t}", "symbol": SYM, "t_sig": t, "c0": px, "q24": 1e8, "qrel": 2.0,
          "cats": {"H1": False, "H2": False, "H3": True, "H4": True, "H5": True}, "flags": {"B": True, "Q": True},
          "limit_px": px * MULT, "limit_until": t + 30 * 60_000, "warmup": False, "done": False, "delisted": False,
          "lim": {"status": "wait", "final": {}}, "site": None}
    tg = None
    if os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"):
        from app.notify import Telegram
        tg = Telegram(os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]); tg.start()
    notes = Notes(OUT + ".txt", tg)
    ex = PaperExchange(m, state=None, save=lambda s: store.put("paper_state:paper_cycle", s))
    eng = Engine(cfg, store, m, OneSignal(ev), reg, notes, [Book(cfg["books"][0], ex, cfg["risk"])])
    print(f"시작 {kst_str(t)} {SYM} 현재가 {px} 지정가 {ev['limit_px']:.6g}  레짐 {reg.snapshot().get('k1')}/{reg.snapshot().get('k7')}", flush=True)
    deadline = t + 45 * 60_000
    while now_ms() < deadline:
        eng.step()
        legs = store.legs("paper_cycle")
        st = " ".join(f"{l['strategy']}{l['leg']}:{l['status']}" for l in legs)
        print(kst_str(now_ms()), st, flush=True)
        if legs and all(l["status"] in ("closed", "cancelled", "rejected") for l in legs):
            break
        time.sleep(5)
    eng.sync_site()
    legs = store.legs("paper_cycle")
    lines = ["\n== 결과 =="]
    for l in legs:
        lines.append(f"{l['strategy']}{l['leg']} {l['status']} 지정가 {l['limit_px']} 체결 {l['fill_px']} × {l['fill_qty']} "
                     f"({kst_str(l['fill_ts'])}, maker={l['entry_maker']}) 청산 {l['exit_reason']} {l['exit_px']} ({kst_str(l['exit_ts'])}) "
                     f"수수료 {(l['entry_fee'] or 0) + (l['exit_fee'] or 0):.4f} 손익 {l['pnl_usdt']} USDT ({l['pnl_pct']}%) 보유 {l['hold_h']}h L{l['lev']}")
    lines.append("결정: " + json.dumps(store.q("SELECT strategy, decision, reason FROM decisions"), ensure_ascii=False))
    lines.append("오류·경고: " + json.dumps(store.q("SELECT kind, msg FROM events WHERE level!='INFO'"), ensure_ascii=False))
    lines.append(f"paper 체결 기록 {len(ex.execs)}건, 남은 주문 {len(ex.open_orders())}, 남은 포지션 {ex.positions()}")
    txt = "\n".join(lines)
    print(txt)
    with open(OUT + ".txt", "a", encoding="utf-8") as f:
        f.write(txt + "\n")
    if tg:
        time.sleep(5)


if __name__ == "__main__":
    main()
