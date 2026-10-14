"""봇 기록 점검 (읽기만): 엔진 살아 있는지, 신호 수신, 결정, 다리 상태, 최근 오류. 사용: python tools/status_check.py"""
import os
import json
import time
import sqlite3

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "bot.db")
c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
c.row_factory = sqlite3.Row
q = lambda s, a=(): [dict(r) for r in c.execute(s, a)]
hb = q("SELECT value FROM kv WHERE key='heartbeat'")
print("엔진 마지막 동작:", f"{time.time() - json.loads(hb[0]['value']) / 1000:.0f}초 전" if hb else "기록 없음")
print("받은 신호:", q("SELECT COUNT(*) n FROM signals")[0]["n"])
print("결정:", {r["decision"]: r["n"] for r in q("SELECT decision, COUNT(*) n FROM decisions GROUP BY decision")})
print("다리 상태:", {r["status"]: r["n"] for r in q("SELECT status, COUNT(*) n FROM legs GROUP BY status")})
for r in q("SELECT reason, COUNT(*) n FROM decisions WHERE decision='skip' GROUP BY reason ORDER BY n DESC LIMIT 6"):
    print(f"  건너뜀 {r['n']:3d}  {r['reason'][:90]}")
print("최근 오류·경고:")
for r in q("SELECT ts, level, kind, msg FROM events WHERE level!='INFO' ORDER BY id DESC LIMIT 8"):
    print(" ", time.strftime("%m-%d %H:%M:%S", time.localtime(r["ts"] / 1000)), r["level"], r["kind"], "-", r["msg"][:160])
tg = q("SELECT value FROM kv WHERE key LIKE '%telegram%'")
