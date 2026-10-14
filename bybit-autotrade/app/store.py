"""
기록 (SQLite, data/bot.db). 검증의 핵심이라 지우지 않고 쌓는다.
- signals   : 스크리너 사건 원본 + 사이트 리플레이 결과 (나중에 실전과 짝 비교)
- decisions : 장부·전략마다 사건을 탔는지/건너뛰었는지와 이유 (모든 신호)
- legs      : 실제(또는 가상) 주문 단위. 신호가·지정가·체결가·슬리피지·수수료·펀딩·손익·보유시간·레짐·사이트 결과
- execs     : 거래소 체결 원본
- events    : 오류·알림·상태 변화 로그
- kv        : 정지 상태, 기준값, paper 거래소 상태 등
"""
import os
import json
import sqlite3
import threading
from .timeutil import now_ms

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals(
  event_id TEXT PRIMARY KEY, symbol TEXT, t_sig INTEGER, c0 REAL, limit_px REAL, limit_until INTEGER,
  cats TEXT, flags TEXT, q24 REAL, qrel REAL, warmup INTEGER, site TEXT, received_ts INTEGER,
  k1 REAL, k7 REAL, site_lim TEXT, site_updated INTEGER);
CREATE TABLE IF NOT EXISTS decisions(
  book TEXT, event_id TEXT, strategy TEXT, decision TEXT, reason TEXT, ts INTEGER, k1 REAL, k7 REAL,
  PRIMARY KEY(book, event_id, strategy));
CREATE TABLE IF NOT EXISTS legs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, book TEXT, mode TEXT, event_id TEXT, strategy TEXT, leg TEXT, symbol TEXT,
  status TEXT, reason TEXT, created_ts INTEGER, updated_ts INTEGER,
  signal_c0 REAL, limit_px REAL, limit_until INTEGER, plan_qty REAL, lev INTEGER, margin REAL, risk_usdt REAL,
  entry_link TEXT, entry_order_id TEXT, fill_ts INTEGER, fill_px REAL, fill_qty REAL, entry_fee REAL DEFAULT 0, entry_maker INTEGER,
  sl_px REAL, tp_px REAL, sl_order_id TEXT, tp_order_id TEXT, armed INTEGER DEFAULT 0, tp_done INTEGER DEFAULT 0,
  max_px REAL, exit_due_ts INTEGER, exit_ts INTEGER, exit_px REAL, exit_qty REAL DEFAULT 0, exit_value REAL DEFAULT 0,
  exit_fee REAL DEFAULT 0, exit_reason TEXT, funding REAL DEFAULT 0, pnl_usdt REAL, pnl_pct REAL, hold_h REAL,
  k1 REAL, k7 REAL, site_key TEXT, site_result REAL, attempts INTEGER DEFAULT 0, extra TEXT,
  UNIQUE(book, event_id, strategy, leg));
CREATE TABLE IF NOT EXISTS execs(
  book TEXT, exec_id TEXT, order_id TEXT, link TEXT, symbol TEXT, side TEXT, qty REAL, price REAL, fee REAL,
  exec_type TEXT, time INTEGER, is_maker INTEGER, stop_type TEXT, leg_id INTEGER, PRIMARY KEY(book, exec_id));
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, level TEXT, book TEXT, kind TEXT, msg TEXT);
CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT);
"""

LEG_ACTIVE = ("entry_open", "open", "closing")


class Store:
    def __init__(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.path = path
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    def q(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def x(self, sql, args=()):
        with self.lock:
            cur = self.db.execute(sql, args)
            return cur.lastrowid

    # ---- kv ----
    def get(self, key, default=None):
        r = self.q("SELECT value FROM kv WHERE key=?", (key,))
        return json.loads(r[0]["value"]) if r else default

    def put(self, key, value):
        self.x("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (key, json.dumps(value)))

    # ---- 로그 ----
    def event(self, level, book, kind, msg):
        self.x("INSERT INTO events(ts,level,book,kind,msg) VALUES(?,?,?,?,?)", (now_ms(), level, book, kind, msg))

    # ---- 신호 ----
    def has_signal(self, eid):
        return bool(self.q("SELECT 1 FROM signals WHERE event_id=?", (eid,)))

    def add_signal(self, e, k1, k7):
        self.x("""INSERT OR IGNORE INTO signals(event_id,symbol,t_sig,c0,limit_px,limit_until,cats,flags,q24,qrel,warmup,site,
                  received_ts,k1,k7) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
               (e["id"], e["symbol"], e["t_sig"], e.get("c0"), e.get("limit_px"), e.get("limit_until"),
                json.dumps(e.get("cats")), json.dumps(e.get("flags")), e.get("q24"), e.get("qrel"), int(bool(e.get("warmup"))),
                json.dumps(e.get("site"), ensure_ascii=False), now_ms(), k1, k7))

    def update_site(self, eid, lim):
        self.x("UPDATE signals SET site_lim=?, site_updated=? WHERE event_id=?", (json.dumps(lim), now_ms(), eid))

    def signal(self, eid):
        r = self.q("SELECT * FROM signals WHERE event_id=?", (eid,))
        return r[0] if r else None

    # ---- 결정 ----
    def decide(self, book, eid, strategy, decision, reason, k1=None, k7=None):
        self.x("INSERT OR IGNORE INTO decisions(book,event_id,strategy,decision,reason,ts,k1,k7) VALUES(?,?,?,?,?,?,?,?)",
               (book, eid, strategy, decision, reason, now_ms(), k1, k7))

    def decided(self, book, eid, strategy):
        return bool(self.q("SELECT 1 FROM decisions WHERE book=? AND event_id=? AND strategy=?", (book, eid, strategy)))

    # ---- 다리(leg) ----
    def add_leg(self, **kw):
        kw.setdefault("created_ts", now_ms())
        kw.setdefault("updated_ts", now_ms())
        cols = ",".join(kw)
        return self.x(f"INSERT INTO legs({cols}) VALUES({','.join('?' * len(kw))})", tuple(kw.values()))

    def upd_leg(self, leg_id, **kw):
        kw["updated_ts"] = now_ms()
        sets = ",".join(f"{k}=?" for k in kw)
        self.x(f"UPDATE legs SET {sets} WHERE id=?", (*kw.values(), leg_id))

    def leg(self, leg_id):
        r = self.q("SELECT * FROM legs WHERE id=?", (leg_id,))
        return r[0] if r else None

    def legs(self, book=None, status=None, where="", args=()):
        sql, a = "SELECT * FROM legs WHERE 1=1", []
        if book:
            sql += " AND book=?"; a.append(book)
        if status:
            sql += f" AND status IN ({','.join('?' * len(status))})"; a += list(status)
        if where:
            sql += " AND " + where; a += list(args)
        return self.q(sql + " ORDER BY id", tuple(a))

    def active_legs(self, book):
        return self.legs(book, LEG_ACTIVE)

    # ---- 체결 ----
    def add_exec(self, book, e, leg_id):
        with self.lock:
            cur = self.db.execute("""INSERT OR IGNORE INTO execs(book,exec_id,order_id,link,symbol,side,qty,price,fee,exec_type,time,
                                     is_maker,stop_type,leg_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                  (book, e.exec_id, e.order_id, e.link, e.symbol, e.side, e.qty, e.price, e.fee, e.exec_type,
                                   e.time, int(e.is_maker), e.stop_type, leg_id))
            return cur.rowcount == 1

    def has_exec(self, book, exec_id):
        return bool(self.q("SELECT 1 FROM execs WHERE book=? AND exec_id=?", (book, exec_id)))
