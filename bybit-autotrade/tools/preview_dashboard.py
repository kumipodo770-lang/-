"""
현황판 미리보기 (주문 없음): paper 점검 기록(db)을 그대로 웹 화면에 띄움. 사용: python tools/preview_dashboard.py <db> <장부이름> <포트>
비밀번호는 환경 변수 PREVIEW_PW (시험용).
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import uvicorn
from app import config as C
from app.store import Store
from app.market import Market
from app.regime import Regime
from app.engine import Engine, Book
from app.web import create_app


class NoScreener:
    def hplan(self, days=2):
        return {"ready": True, "events": []}


class NoEx:
    mode = "paper"

    def tick(self, *a):
        pass


db, book, port = sys.argv[1], sys.argv[2], int(sys.argv[3])
cfg = C._merge(C.DEFAULTS, {})
budgets = next((b["budgets"] for b in C.load()["books"] if b["name"] == book), {"H3": 180, "H4": 350, "H5": 180})
cfg["books"] = [{"name": book, "mode": "paper", "budgets": budgets}]
C.validate(cfg)
store = Store(db)
m = Market()
reg = Regime(m); reg.refresh(force=True)
eng = Engine(cfg, store, m, NoScreener(), reg, None, [Book(cfg["books"][0], NoEx(), cfg["risk"])])
store.put("heartbeat", __import__("time").time() * 1000)
app = create_app(eng, store, os.environ["PREVIEW_PW"], C.public_view(cfg), None, m)
uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
