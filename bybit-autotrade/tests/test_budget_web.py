"""웹 예산 수정: 설정 파일 한 줄만 바뀜, 최소 금액·잔고 검사, 올릴 때 비밀번호 다시, 엔진 적용·알림"""
import os
import pytest
from fastapi.testclient import TestClient
from app import config as C
from app.web import create_app
from app.store import Store
from test_engine import build

CFG_TEXT = """# 맨 위 주석
risk:
  per_trade_risk_pct: 0.25         # 주석 유지
books:
  - name: paper
    mode: paper                    # 가상
    budgets: {H3: 180, H4: 350, H5: 180}       # 최소 금액 주석
    regime_gate: {k1_down: record, low_vol: record}
#  - name: live
#    mode: live
#    budgets: {H3: 180, H4: 350, H5: 180}
"""


def test_write_budgets_changes_only_that_line(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(CFG_TEXT, encoding="utf-8")
    C.write_budgets(str(p), "paper", {"H3": 300, "H4": 0, "H5": 180.5}, backup_dir=str(tmp_path / "bk"))
    new = p.read_text(encoding="utf-8")
    old_lines, new_lines = CFG_TEXT.split("\n"), new.split("\n")
    changed = [i for i, (a, b) in enumerate(zip(old_lines, new_lines)) if a != b]
    assert len(changed) == 1 and "budgets: {H3: 300, H4: 0, H5: 180.5}" in new_lines[changed[0]]
    assert "# 최소 금액 주석" in new_lines[changed[0]]
    assert "#    budgets: {H3: 180, H4: 350, H5: 180}" in new          # 주석 처리된 live 줄은 그대로
    assert C.load(str(p))["books"][0]["budgets"] == {"H3": 300, "H4": 0, "H5": 180.5}
    assert len(os.listdir(tmp_path / "bk")) == 1
    with pytest.raises(C.ConfigError):
        C.write_budgets(str(p), "nobook", {"H3": 1})


@pytest.fixture
def eng_cfgfile(cfg, tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(CFG_TEXT, encoding="utf-8")
    cfg["books"] = C.load(str(p))["books"]
    eng, store, ex, market, scr, clock = build(cfg, tmp_path)
    eng.cfg_path, eng.cfg_mtime = str(p), eng._mtime()
    return eng, store, p


def test_engine_budget_validation_and_apply(eng_cfgfile):
    eng, store, p = eng_cfgfile
    assert not eng.check_budget_change("paper", {"H3": 100})[0]              # 최소 미만
    assert not eng.check_budget_change("paper", {"XX": 500})[0]
    assert not eng.check_budget_change("paper", {"H3": 180})[0]              # 바뀐 것 없음
    assert not eng.check_budget_change("nobook", {"H3": 500})[0]
    ok, msg, diff, up = eng.check_budget_change("paper", {"H3": 400, "H5": 0})
    assert ok and up and ("H3", 180.0, 400.0) in diff and ("H5", 180.0, 0.0) in diff
    assert eng.request_budgets("paper", {"H3": 400, "H5": 0}).startswith("접수")
    eng.step()
    b = eng.books[0]
    assert b.budgets["H3"] == 400 and b.budgets["H5"] == 0
    assert C.load(str(p))["books"][0]["budgets"] == {"H3": 400, "H4": 350, "H5": 0}
    assert any("예산 변경 (웹)" in m for m in eng.tg.msgs)
    assert store.q("SELECT * FROM events WHERE kind='예산 변경(웹)'")
    eng.step()                                                              # 파일 감시가 또 알리지 않음
    assert sum("설정 변경 감지" in m for m in eng.tg.msgs) == 0


def test_live_increase_limited_by_equity(eng_cfgfile):
    eng, store, p = eng_cfgfile
    b = eng.books[0]
    b.mode = "live"
    b.last_equity = None
    assert "잔고" in eng.check_budget_change("paper", {"H3": 400})[1]        # 잔고 모르면 올리기 불가
    assert eng.check_budget_change("paper", {"H3": 0})[0]                    # 내리기는 가능
    b.last_equity = 800.0
    assert not eng.check_budget_change("paper", {"H3": 400})[0]              # 400+350+180 = 930 > 800
    assert eng.check_budget_change("paper", {"H3": 250})[0]                  # 780 ≤ 800


def test_web_budget_flow_needs_password_to_raise(eng_cfgfile):
    eng, store, p = eng_cfgfile
    cl = TestClient(create_app(eng, store, "dash-password-1", {}))
    assert cl.post("/api/budget/confirm", json={"book": "paper", "budgets": {"H3": 500}}).status_code == 401
    r = cl.post("/login", data={"password": "dash-password-1"}, follow_redirects=False)
    cl.cookies.set("at_session", r.cookies["at_session"])
    r = cl.post("/api/budget/confirm", json={"book": "paper", "budgets": {"H3": 100}})
    assert r.status_code == 400 and "최소" in r.json()["detail"]
    j = cl.post("/api/budget/confirm", json={"book": "paper", "budgets": {"H3": 500}}).json()
    assert j["needs_password"] and j["diff"] == [{"s": "H3", "old": 180.0, "new": 500.0}]
    assert cl.post("/api/action", json={"nonce": j["nonce"]}).status_code == 400            # 다른 버튼으로 못 씀
    j = cl.post("/api/budget/confirm", json={"book": "paper", "budgets": {"H3": 500}}).json()
    assert cl.post("/api/budget/apply", json={"nonce": j["nonce"], "password": "wrong"}).status_code == 403
    j = cl.post("/api/budget/confirm", json={"book": "paper", "budgets": {"H3": 500}}).json()
    assert cl.post("/api/budget/apply", json={"nonce": j["nonce"], "password": "dash-password-1"}).json()["msg"].startswith("접수")
    eng.step()
    assert eng.books[0].budgets["H3"] == 500
    # 내리기는 비밀번호 없이
    j = cl.post("/api/budget/confirm", json={"book": "paper", "budgets": {"H3": 200}}).json()
    assert not j["needs_password"]
    assert cl.post("/api/budget/apply", json={"nonce": j["nonce"]}).json()["msg"].startswith("접수")
    eng.step()
    assert eng.books[0].budgets["H3"] == 200 and C.load(str(p))["books"][0]["budgets"]["H3"] == 200
