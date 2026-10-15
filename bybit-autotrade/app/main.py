"""
실행: .venv\\Scripts\\python -m app.main   (run_bot.bat)
장부(books)마다 거래소를 만든다: paper = 가상 체결 / demo = 바이비트 데모 트레이딩 / live = 실계좌.
live 는 config.live_allowed() 를 통과할 때만 켜진다 (키 + LIVE_CONFIRM.txt). 아니면 그 장부는 꺼진 채 이유만 표시.
"""
import os
import json
import logging
import threading
from logging.handlers import RotatingFileHandler
from . import config as C
from .store import Store
from .market import Market
from .screener import Screener
from .regime import Regime
from .engine import Engine, Book
from .exchange_paper import PaperExchange
from .notify import Telegram

DATA = os.path.join(C.ROOT, "data")


def setup_logging():
    os.makedirs(DATA, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    h = RotatingFileHandler(os.path.join(DATA, "bot.log"), maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    h.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers = [h, sh]
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def build_books(cfg, store, market):
    books = []
    r = cfg["risk"]
    for bc in cfg["books"]:
        name, mode = bc["name"], bc["mode"]
        ex, enabled, why = None, True, ""
        if mode == "paper":
            key = f"paper_state:{name}"
            raw = store.get(key)
            ex = PaperExchange(market, r["fee_taker_pct"], r["fee_maker_pct"], cfg["paper"]["slippage_pct"],
                               cfg["paper"]["start_equity"], state=json.loads(raw) if raw else None,
                               save=lambda s, k=key: store.put(k, s))
        elif mode == "demo":
            k, s = C.secret("BYBIT_DEMO_API_KEY"), C.secret("BYBIT_DEMO_API_SECRET")
            if k and s:
                from .exchange_bybit import BybitExchange
                ex = BybitExchange("demo", k, s, market, retries=r["api_fail_retries"])
            else:
                enabled, why = False, "demo 키 환경 변수 없음 (BYBIT_DEMO_API_KEY/SECRET)"
        elif mode == "live":
            ok, msg = C.live_allowed()
            if ok:
                from .exchange_bybit import BybitExchange
                ex = BybitExchange("live", C.secret("BYBIT_LIVE_API_KEY"), C.secret("BYBIT_LIVE_API_SECRET"), market,
                                   retries=r["api_fail_retries"])
            else:
                enabled, why = False, f"live 꺼짐: {msg}"
        books.append(Book(bc, ex, r, enabled, why))
    return books


def main():
    setup_logging()
    C.load_dotenv()
    cfg = C.load()
    log = logging.getLogger("main")
    store = Store(os.path.join(DATA, "bot.db"))
    market = Market(cfg["market"]["public_url"])
    screener = Screener(cfg["screener"]["url"], C.secret("SCREENER_PASSWORD"))
    regime = Regime(market)
    regime.refresh(force=True)
    books = build_books(cfg, store, market)
    tg = None
    if cfg["telegram"]["enabled"] and C.secret("TELEGRAM_BOT_TOKEN"):
        tg = Telegram(C.secret("TELEGRAM_BOT_TOKEN"), C.secret("TELEGRAM_CHAT_ID"))
    eng = Engine(cfg, store, market, screener, regime, tg, books, cfg_path=os.path.join(C.ROOT, "config.yaml"))
    if tg:
        tg.on_command = eng.command
        tg.start()
        log.info("텔레그램: 토큰 있음, chat id %s", "있음" if tg.chat_id else "없음 → 봇 대화방에서 /start 를 보내면 답장으로 알려 줌")
    else:
        log.info("텔레그램: 꺼짐 (TELEGRAM_BOT_TOKEN 없음)")
    for b in books:
        log.info("장부 %s (%s) %s %s", b.name, b.mode, "켜짐" if b.enabled else "꺼짐", b.disabled_reason)
    try:                                           # 출금 설정 문제로 매매가 멈추면 안 됨
        from .withdraw import make_withdrawer
        wd, wd_note = make_withdrawer(cfg, books, store, eng.notify, C.secret)
    except Exception as e:
        wd, wd_note = None, f"꺼짐: 설정 오류 {type(e).__name__}"
    log.info("출금: %s", wd_note)
    t = threading.Thread(target=eng.run_forever, daemon=True, name="engine")
    t.start()
    pw = C.secret("DASH_PASSWORD")
    if pw and len(pw) >= 10:
        import uvicorn
        from .web import create_app
        app = create_app(eng, store, pw, C.public_view(cfg), tg, market, withdrawer=wd, withdraw_note=wd_note)
        uvicorn.run(app, host=cfg["web"]["host"], port=int(cfg["web"]["port"]), log_level="warning")
    else:
        log.warning("DASH_PASSWORD 없음 또는 10자 미만 → 웹 화면 꺼짐 (텔레그램·엔진은 동작)")
        t.join()


if __name__ == "__main__":
    main()
