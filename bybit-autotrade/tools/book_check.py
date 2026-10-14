"""장부 하나의 주문·결정·체결 기록 요약 (읽기만). 사용: python tools/book_check.py live"""
import os
import sys
import sqlite3

book = sys.argv[1] if len(sys.argv) > 1 else "live"
DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "bot.db")
c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
print(f"[{book}] 다리(주문):", c.execute("SELECT status, COUNT(*) FROM legs WHERE book=? GROUP BY status", (book,)).fetchall())
print(f"[{book}] 결정:", c.execute("SELECT decision, COUNT(*) FROM decisions WHERE book=? GROUP BY decision", (book,)).fetchall())
for r in c.execute("SELECT reason, COUNT(*) n FROM decisions WHERE book=? GROUP BY reason ORDER BY n DESC LIMIT 5", (book,)):
    print("  ", r[1], r[0][:80])
print(f"[{book}] 거래소 체결 기록:", c.execute("SELECT COUNT(*) FROM execs WHERE book=?", (book,)).fetchone()[0])
print(f"[{book}] 오류:", c.execute("SELECT kind, COUNT(*) FROM events WHERE book=? AND level='ERROR' GROUP BY kind", (book,)).fetchall())
