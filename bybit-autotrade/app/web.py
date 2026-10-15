"""
휴대폰 화면 (반응형 웹). 로그인 필수 (비밀번호 = 환경 변수 DASH_PASSWORD, 10자 이상).
- 보기: 장부별 상태·오늘/누적 손익, 보유 포지션, 최근 신호와 진입/건너뜀 이유, 오류 로그, 레짐.
- 버튼: 정지 / 재개 / 전체 청산 — 누르면 확인 창(60초 유효 확인 코드) → 한 번 더 눌러야 실행.
- 차트: 포지션 줄을 누르면 그 코인 캔들(바이비트 공개 시세) + 지정가·체결가·손절·익절 선 + 진입/청산 표시. 보유 중이면 15초마다 갱신.
  방법별 누적 손익 곡선. 차트 그리기는 lightweight-charts 4.2.0 (unpkg, 휴대폰 브라우저가 받음).
- 예산 수정 (사용자 결정 2026-10-13): 세 종류 예산 칸만. 확인 단계 → 올릴 때는 화면 비밀번호 다시 → config.yaml 한 줄 저장 + 텔레그램 알림.
  최소 금액 미만 불가, 실계좌는 합계 ≤ 계좌 USDT. 한도 %·레버리지·모드(실계좌 켜기)는 여전히 설정 파일에서만.
- 출금 (사용자 결정 2026-10-15): 화이트리스트에 넣은 업비트 XRP 주소 1개로만. 달러 금액 → 미리 보기(보낼 XRP·통장 예상 원화)
  → 화면 비밀번호 다시 → 실행 + 텔레그램 알림. 받는 주소는 설정 파일에만 있고 화면에서 바꿀 수 없다 (app/withdraw.py).
- 로그인 5번 틀리면 그 주소는 15분 잠금.
"""
import io
import csv
import time
import hmac
import secrets
from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool
from .timeutil import kst_str, now_ms
from .withdraw import WithdrawError

SESSION_SEC = 7 * 86400
KST_SHIFT = 9 * 3600          # 차트 시간축을 한국 시간으로 보이게 (라이브러리는 UTC 로 그림)
IVS = {"1": 60_000, "15": 900_000, "60": 3_600_000}


def create_app(engine, store, password, cfg_public, telegram=None, market=None, withdrawer=None, withdraw_note="꺼짐"):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    sessions, fails, nonces = {}, {}, {}
    kcache = {}

    def klines_cached(symbol, iv, start, end):
        key = (symbol, iv, start // 10_000, end // 10_000)
        hit = kcache.get(key)
        if hit and time.time() - hit[0] < 10:
            return hit[1]
        rows = market.klines(symbol, iv, start=start, end=end, limit=1000) if market else []
        if len(kcache) > 200:
            kcache.clear()
        kcache[key] = (time.time(), rows)
        return rows

    def authed(req: Request):
        tok = req.cookies.get("at_session")
        exp = sessions.get(tok or "")
        if not exp or exp < time.time():
            raise HTTPException(401, "로그인 필요")
        return True

    @app.get("/login", response_class=HTMLResponse)
    def login_page(err: int = 0):
        return LOGIN_HTML.replace("%ERR%", "비밀번호가 틀렸거나 잠겼습니다" if err else "")

    @app.post("/login")
    async def login(req: Request):
        ip = req.client.host if req.client else "?"
        f = fails.get(ip, [0, 0])
        if f[0] >= 5 and time.time() < f[1]:
            return RedirectResponse("/login?err=1", 303)
        form = await req.form()
        pw = str(form.get("password", ""))
        if password and hmac.compare_digest(pw, password):
            fails.pop(ip, None)
            tok = secrets.token_urlsafe(32)
            sessions[tok] = time.time() + SESSION_SEC
            r = RedirectResponse("/", 303)
            r.set_cookie("at_session", tok, max_age=SESSION_SEC, httponly=True, samesite="strict")
            return r
        f = [f[0] + 1, time.time() + 900]
        fails[ip] = f
        return RedirectResponse("/login?err=1", 303)

    @app.get("/logout")
    def logout(req: Request):
        sessions.pop(req.cookies.get("at_session", ""), None)
        return RedirectResponse("/login", 303)

    @app.get("/", response_class=HTMLResponse)
    def index(req: Request):
        try:
            authed(req)
        except HTTPException:
            return RedirectResponse("/login", 303)
        return DASH_HTML

    def withdraw_status():
        if not withdrawer:
            return {"enabled": False, "reason": withdraw_note}
        try:
            return withdrawer.status()
        except Exception as e:                     # 출금 쪽 오류가 현황판 전체를 막지 않게
            return {"enabled": False, "reason": f"출금 상태 오류 {type(e).__name__}"}

    @app.get("/api/status")
    def status(req: Request):
        authed(req)
        snap = engine.snapshot()
        legs = store.q("SELECT * FROM legs WHERE status IN ('entry_open','open','closing') ORDER BY id DESC")
        closed = store.q("SELECT * FROM legs WHERE status='closed' ORDER BY exit_ts DESC LIMIT 30")
        dec = store.q("""SELECT d.*, s.symbol, s.t_sig FROM decisions d LEFT JOIN signals s ON s.event_id=d.event_id
                         ORDER BY d.ts DESC LIMIT 60""")
        evs = store.q("SELECT * FROM events WHERE level IN ('ERROR','WARN') ORDER BY id DESC LIMIT 30")
        for r in legs + closed:
            for k in ("fill_ts", "exit_ts", "exit_due_ts", "limit_until", "created_ts"):
                r[k + "_s"] = kst_str(r.get(k))
        for r in dec:
            r["ts_s"] = kst_str(r["ts"])
        for r in evs:
            r["ts_s"] = kst_str(r["ts"])
        hb = store.get("heartbeat")
        return JSONResponse({"snap": snap, "legs": legs, "closed": closed, "decisions": dec, "events": evs,
                             "heartbeat_s": kst_str(hb), "heartbeat_age": (time.time() * 1000 - hb) / 1000 if hb else None,
                             "config": cfg_public,
                             "withdraw": withdraw_status(),
                             "telegram": {"enabled": bool(telegram and telegram.ok), "chat_set": bool(telegram and telegram.chat_id),
                                          "seen_chat": telegram.seen_chat if telegram else None,
                                          "error": telegram.last_error if telegram else None}})

    @app.post("/api/confirm")
    async def confirm(req: Request):
        authed(req)
        body = await req.json()
        cmd, book = body.get("cmd"), body.get("book", "all")
        if cmd not in ("stop", "resume", "closeall"):
            raise HTTPException(400, "명령 없음")
        n = secrets.token_hex(8)
        nonces[n] = (cmd, book, time.time() + 60)
        return {"nonce": n, "cmd": cmd, "book": book}

    @app.post("/api/action")
    async def action(req: Request):
        authed(req)
        body = await req.json()
        item = nonces.pop(body.get("nonce", ""), None)
        if not item or item[0] not in ("stop", "resume", "closeall") or item[2] < time.time():
            raise HTTPException(400, "확인 시간이 지났습니다")
        return {"msg": engine.command(item[0], item[1])}

    # ---- 예산 수정 (사용자 결정 2026-10-13): 확인 단계 → (올리면 비밀번호 다시) → 적용 ----
    @app.post("/api/budget/confirm")
    async def budget_confirm(req: Request):
        authed(req)
        body = await req.json()
        book, budgets = body.get("book"), body.get("budgets") or {}
        ok, msg, diff, up = engine.check_budget_change(book, budgets)
        if not ok:
            raise HTTPException(400, msg)
        n = secrets.token_hex(8)
        nonces[n] = ("budget", (book, {s: v for s, _, v in diff}, up), time.time() + 120)
        return {"nonce": n, "diff": [{"s": s, "old": o, "new": v} for s, o, v in diff], "needs_password": up}

    @app.post("/api/budget/apply")
    async def budget_apply(req: Request):
        authed(req)
        ip = req.client.host if req.client else "?"
        body = await req.json()
        item = nonces.pop(body.get("nonce", ""), None)
        if not item or item[0] != "budget" or item[2] < time.time():
            raise HTTPException(400, "확인 시간이 지났습니다. 다시 해 주세요")
        book, budgets, up = item[1]
        if up:
            f = fails.get(ip, [0, 0])
            if f[0] >= 5 and time.time() < f[1]:
                raise HTTPException(403, "비밀번호를 여러 번 틀려 잠겼습니다 (15분)")
            if not hmac.compare_digest(str(body.get("password", "")), password):
                fails[ip] = [f[0] + 1, time.time() + 900]
                raise HTTPException(403, "비밀번호가 틀렸습니다")
            fails.pop(ip, None)
        return {"msg": engine.request_budgets(book, budgets)}

    # ---- 출금 (사용자 결정 2026-10-15): 미리 보기 → 화면 비밀번호 다시 → 실행 ----
    def check_password(ip, pw):
        f = fails.get(ip, [0, 0])
        if f[0] >= 5 and time.time() < f[1]:
            raise HTTPException(403, "비밀번호를 여러 번 틀려 잠겼습니다 (15분)")
        if not hmac.compare_digest(str(pw or ""), password):
            fails[ip] = [f[0] + 1, time.time() + 900]
            raise HTTPException(403, "비밀번호가 틀렸습니다")
        fails.pop(ip, None)

    @app.post("/api/withdraw/preview")
    async def withdraw_preview(req: Request):
        authed(req)
        if not withdrawer:
            raise HTTPException(400, f"출금 기능 {withdraw_note}")
        body = await req.json()
        try:
            p = await run_in_threadpool(withdrawer.preview, body.get("usd"))
        except WithdrawError as e:
            raise HTTPException(400, str(e))
        except Exception as e:
            raise HTTPException(400, f"바이비트·업비트 조회 오류: {str(e)[:200]}")
        n = secrets.token_hex(8)
        nonces[n] = ("withdraw", p, time.time() + 120)
        return {"nonce": n, "plan": p}

    @app.post("/api/withdraw/apply")
    async def withdraw_apply(req: Request):
        authed(req)
        ip = req.client.host if req.client else "?"
        body = await req.json()
        item = nonces.pop(body.get("nonce", ""), None)
        if not item or item[0] != "withdraw" or item[2] < time.time() or not withdrawer:
            raise HTTPException(400, "확인 시간이 지났습니다. 다시 계산해 주세요")
        check_password(ip, body.get("password"))
        try:
            msg = await run_in_threadpool(withdrawer.execute, item[1])
        except WithdrawError as e:
            raise HTTPException(400, str(e))
        except Exception as e:
            raise HTTPException(400, f"출금 결과 확인 필요 ({type(e).__name__}) — 텔레그램과 바이비트 앱 출금 내역을 확인하세요")
        return {"msg": msg}

    @app.get("/api/chart")
    def chart(req: Request, leg_id: int, iv: str = "15"):
        authed(req)
        if iv not in IVS:
            raise HTTPException(400, "간격은 1·15·60")
        l = store.leg(leg_id)
        if not l:
            raise HTTPException(404, "없음")
        step = IVS[iv]
        t_from = (l.get("fill_ts") or l["created_ts"]) - 60 * step
        t_to = (l.get("exit_ts") or now_ms()) + 20 * step
        t_to = min(t_to, now_ms())
        t_from = max(t_from, t_to - 990 * step)
        rows = klines_cached(l["symbol"], iv, t_from, t_to)
        sh = lambda ms: int(ms // 1000) + KST_SHIFT
        candles = [{"time": sh(r[0]), "open": r[1], "high": r[2], "low": r[3], "close": r[4]} for r in rows]
        lines = []
        for price, color, title in ((l.get("limit_px"), "#8b949e", "지정가"), (l.get("fill_px"), "#58a6ff", "체결"),
                                    (l.get("sl_px"), "#f85149", "손절"), (l.get("tp_px"), "#3fb950", "익절")):
            if price:
                lines.append({"price": price, "color": color, "title": title})
        markers = []
        bar = lambda ms: sh((ms // step) * step)
        if l.get("fill_ts"):
            markers.append({"time": bar(l["fill_ts"]), "position": "belowBar", "color": "#58a6ff", "shape": "arrowUp", "text": "진입"})
        if l.get("exit_ts"):
            good = (l.get("pnl_pct") or 0) >= 0
            markers.append({"time": bar(l["exit_ts"]), "position": "aboveBar", "color": "#3fb950" if good else "#f85149",
                            "shape": "arrowDown", "text": f"{l.get('exit_reason') or '청산'} {l.get('pnl_pct') or 0:+.2f}%"})
        title = f"{l['symbol']} · {l['strategy']}{l['leg']} · {l['status']}"
        return {"title": title, "candles": candles, "lines": lines, "markers": sorted(markers, key=lambda m: m["time"]),
                "live": l["status"] in ("entry_open", "open", "closing")}

    @app.get("/api/equity")
    def equity(req: Request):
        authed(req)
        out = {}
        for l in store.q("SELECT book, strategy, exit_ts, pnl_usdt FROM legs WHERE status='closed' AND pnl_usdt IS NOT NULL ORDER BY exit_ts"):
            k = f"{l['book']} {l['strategy']}"
            s = out.setdefault(k, {"pts": [], "cum": 0.0})
            s["cum"] += l["pnl_usdt"]
            t = int(l["exit_ts"] // 1000) + KST_SHIFT
            if s["pts"] and s["pts"][-1]["time"] >= t:
                t = s["pts"][-1]["time"] + 1
            s["pts"].append({"time": t, "value": round(s["cum"], 6)})
        return {k: v["pts"] for k, v in out.items()}

    @app.get("/api/export.csv")
    def export(req: Request):
        authed(req)
        rows = store.q("""SELECT l.*, s.t_sig, s.c0 AS sig_c0, s.qrel, s.q24 FROM legs l LEFT JOIN signals s ON s.event_id=l.event_id ORDER BY l.id""")
        buf = io.StringIO()
        if rows:
            w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                                 headers={"Content-Disposition": "attachment; filename=legs.csv"})

    return app


LOGIN_HTML = """<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>자동매매 로그인</title><style>
:root{--bg:#0d1117;--card:#161b22;--fg:#e6edf3;--mut:#8b949e;--acc:#3fb950;--bad:#f85149;--line:#30363d}
body{margin:0;background:var(--bg);color:var(--fg);font-family:system-ui,'Malgun Gothic',sans-serif;display:flex;min-height:100vh;align-items:center;justify-content:center;padding:16px}
form{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:28px 22px;width:100%;max-width:340px}
h1{font-size:18px;margin:0 0 18px}input{width:100%;box-sizing:border-box;padding:12px;border-radius:6px;border:1px solid var(--line);background:#0b0f14;color:var(--fg);font-size:16px}
button{margin-top:14px;width:100%;padding:12px;border-radius:6px;border:1px solid var(--acc);background:transparent;color:var(--acc);font-size:15px}
.e{color:var(--bad);font-size:13px;margin-top:10px;min-height:16px}</style></head><body>
<form method="post" action="/login"><h1>🔒 자동매매 검증</h1><input type="password" name="password" placeholder="비밀번호" autofocus autocomplete="current-password">
<button>들어가기</button><div class="e">%ERR%</div></form></body></html>"""

DASH_HTML = """<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>자동매매 검증</title><style>
:root{--bg:#0d1117;--card:#161b22;--fg:#e6edf3;--mut:#8b949e;--good:#3fb950;--bad:#f85149;--warn:#d29922;--line:#30363d}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font-family:system-ui,'Malgun Gothic',sans-serif;font-size:14px}
header{position:sticky;top:0;background:var(--bg);border-bottom:1px solid var(--line);padding:10px 16px;display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap}
h1{font-size:16px;margin:0}main{padding:12px 16px 40px;max-width:1100px;margin:0 auto}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px;margin:10px 0}
h2{font-size:14px;margin:0 0 8px;color:var(--mut);font-weight:600}
.row{display:flex;flex-wrap:wrap;gap:8px}.pill{border:1px solid var(--line);border-radius:999px;padding:3px 10px;font-size:12px}
.good{color:var(--good)}.bad{color:var(--bad)}.warn{color:var(--warn)}.mut{color:var(--mut)}
.tbl{width:100%;border-collapse:collapse;font-size:12px}.tbl td,.tbl th{border-bottom:1px solid var(--line);padding:6px 4px;text-align:left;white-space:nowrap}
.scroll{overflow-x:auto}button{padding:9px 14px;border-radius:8px;border:1px solid var(--line);background:#21262d;color:var(--fg);font-size:14px}
button.stop{border-color:var(--warn);color:var(--warn)}button.close{border-color:var(--bad);color:var(--bad)}button.go{border-color:var(--good);color:var(--good)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:8px}.kpi{border:1px solid var(--line);border-radius:8px;padding:8px}
.kpi b{display:block;font-size:16px;margin-top:2px}#dlg,#bdlg{position:fixed;inset:0;background:#000a;display:none;align-items:center;justify-content:center;padding:16px}
#dlg .box,#bdlg .box{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px;max-width:360px;width:100%}
#bdlg input{background:#0b0f14;border:1px solid var(--line);color:var(--fg);border-radius:6px;padding:8px;font-size:16px}
#bdlg label.ln{align-items:center;margin-top:8px}
.rk{display:inline-block;border:1px solid #58a6ff;color:#58a6ff;border-radius:6px;padding:0 6px;font-size:12px}
.kpi b.inl{display:inline;font-size:15px;margin:0}.sm{font-size:12px}.ln{display:flex;justify-content:space-between;gap:8px;margin-top:3px}
.ln b{display:inline;font-size:14px;margin:0}.kpi.tot{margin-top:8px;border-color:#58a6ff}
#chart{height:320px}#eq{height:220px}.tbl tr.click{cursor:pointer}.tbl tr.click:hover{background:#1f2a37}.tbl tr.sel{background:#1f2a37}
.ivs button{padding:4px 12px;font-size:12px;margin-bottom:8px}.ivs button.on{border-color:#58a6ff;color:#58a6ff}
</style><script src="https://unpkg.com/lightweight-charts@4.2.0/dist/lightweight-charts.standalone.production.js"></script></head><body>
<header><h1>🤖 자동매매 검증</h1><div class="row"><span id="hb" class="pill mut">-</span><a href="/api/export.csv" class="pill mut">CSV</a><a href="/logout" class="pill mut">나가기</a></div></header>
<main><div id="books"></div>
<div class="card" id="chartcard"><h2 id="ct">차트 — 아래 표의 줄을 누르면 그 코인 차트가 나옵니다</h2>
<div class="row ivs"><button data-iv="1" onclick="setIv('1')">1분</button><button data-iv="15" class="on" onclick="setIv('15')">15분</button><button data-iv="60" onclick="setIv('60')">1시간</button>
<span class="mut" style="font-size:12px;align-self:center">회색 지정가 · 파랑 체결 · 빨강 손절 · 초록 익절</span></div><div id="chart"></div></div>
<div class="card"><h2>방법별 누적 손익 (USDT, 청산 기준) <span id="eqnote" class="mut"></span></h2><div id="eq"></div></div>
<div class="card"><h2>보유·대기 주문</h2><div class="scroll"><table class="tbl" id="legs"></table></div></div>
<div class="card"><h2>최근 신호와 결정 (진입/건너뜀 이유)</h2><div class="scroll"><table class="tbl" id="dec"></table></div></div>
<div class="card"><h2>최근 청산 (사이트 결과와 짝)</h2><div class="scroll"><table class="tbl" id="closed"></table></div></div>
<div class="card"><h2>오류·경고</h2><div class="scroll"><table class="tbl" id="evs"></table></div></div>
<div class="card mut" id="tg"></div></main>
<div id="dlg"><div class="box"><p id="dlgt"></p><div class="row"><button class="close" id="ok">확인</button><button id="no">취소</button></div></div></div>
<div id="bdlg"><div class="box" id="bbox"></div></div>
<script>
const $=s=>document.querySelector(s);const f=(x,d=2)=>x==null?'-':(+x).toFixed(d);
const cls=v=>v==null?'':(v>=0?'good':'bad');const esc=s=>String(s??'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
let lastSnap=null,lastWd=null;
async function load(){const r=await fetch('/api/status');if(r.status==401){location='/login';return}const d=await r.json();lastSnap=d.snap;lastWd=d.withdraw;
 const age=d.heartbeat_age;$('#hb').textContent='엔진 '+(age==null?'-':(age<30?'정상 ':'⚠ ')+Math.round(age)+'초 전');$('#hb').className='pill '+(age!=null&&age<30?'good':'bad');
 const rg=d.snap.regime||{};let h='';
 const k1c=rg.k1==1?'good':rg.k1==-1?'bad':'warn',k7c=rg.k7==-1?'bad':'good';
 h+=`<div class="card"><h2>레짐 (BTC 일봉, 매일 오후 1시 갱신)</h2><div class="row"><span class="pill ${k1c}">K1 ${rg.k1==1?'상승':rg.k1==0?'횡보':rg.k1==-1?'하락':'-'}</span><span class="pill ${k7c}">변동성 ${rg.k7==1?'고':rg.k7==0?'중':rg.k7==-1?'저':'-'} (${f(rg.rv_pct,0)}백분위)</span></div>`
  +(rg.explain||[]).map(x=>`<div class="mut sm" style="margin-top:6px">${esc(x)}</div>`).join('')
  +`<div class="mut sm" style="margin-top:6px">상승 = 종가가 50일 평균 위 + 평균이 오르는 중 · 하락 = 둘 다 반대 · 그 밖 = 횡보 / 저변동 = 30일 변동성이 지난 1년 중 아래 1/3</div></div>`;
 for(const b of d.snap.books){const st=!b.enabled?`<span class="bad">꺼짐: ${esc(b.disabled_reason)}</span>`:b.halt?`<span class="warn">정지: ${esc(b.halt)}</span>`:b.blocked?`<span class="bad">진입 막힘: ${esc(b.blocked)}</span>`:'<span class="good">작동 중</span>';
  const md=b.mode=='paper'?'가상 돈 (paper)':b.mode=='live'?'실계좌 (live)':'데모';
  const eqtxt=b.mode=='paper'?'':` · 바이비트 USDT 잔고 ${b.equity==null?'확인 중':f(b.equity)}`;
  const gt=b.gate||{};const gtxt=b.gate_state?`<div class="sm" style="margin-bottom:6px">지금 진입: <b class="${b.gate_state=='열림'?'good':'bad'}" style="display:inline">${esc(b.gate_state)}</b> <span class="mut">(막는 레짐: ${gt.k1_down=='block'?'하락장 ':''}${gt.low_vol=='block'?'저변동':''})</span></div>`:'';
  h+=`<div class="card"><h2>[${esc(b.name)}] ${md} — ${st}${eqtxt}</h2>${gtxt}<div class="grid">`;
  for(const [k,v] of Object.entries(b.strategies)){
   if(v.note){h+=`<div class="kpi"><span class="rk">${v.rank}</span> <b class="inl">${k}</b><div class="mut sm">${esc(v.desc)}</div><b class="warn" style="font-size:13px">${esc(v.note)}</b></div>`;continue}
   h+=`<div class="kpi"><span class="rk">${v.rank}</span> <b class="inl">${k}</b> <span class="mut sm">예산 ${v.budget} (최소 ${v.min_budget})</span><div class="mut sm">${esc(v.desc)}</div>
    <div class="ln"><span>지금 들어가 있는 금액</span><b>${f(v.margin)}</b></div>
    <div class="ln mut sm"><span>포지션 ${f(v.notional)} · ${v.n_open}개${v.n_pending?` · 대기 주문 ${v.n_pending}건 (${f(v.pending_margin)} 예약)`:''}</span></div>
    <div class="ln"><span>이익</span><b class="good">${f(v.profit,3)}</b></div>
    <div class="ln"><span>손실</span><b class="bad">${f(v.loss,3)}</b></div>
    <div class="ln"><span>순손익</span><b class="${cls(v.net)}">${f(v.net,3)}</b></div>
    <div class="ln"><span>보유 중 평가손익</span><b class="${cls(v.upnl)}">${f(v.upnl,3)}</b></div>
    <div class="mut sm">청산 ${v.n_closed}건 (이익 ${v.n_win} · 손실 ${v.n_loss}) · 오늘 ${f(v.today,3)}</div>${v.halt?'<div class="bad sm">⛔'+esc(v.halt)+'</div>':''}</div>`}
  const t=b.total||{};
  h+=`</div><div class="kpi tot"><b class="inl">세 종류 합계</b> <span class="mut sm">예산 ${f(t.budget,0)} USDT</span>
    <div class="ln"><span>지금 들어가 있는 금액</span><b>${f(t.margin)}</b></div>
    <div class="ln mut sm"><span>포지션 ${f(t.notional)} · ${t.n_open}개${t.n_pending?` · 대기 주문 ${t.n_pending}건 (${f(t.pending_margin)} 예약)`:''}</span></div>
    <div class="ln"><span>총 이익</span><b class="good">${f(t.profit,3)}</b></div>
    <div class="ln"><span>총 손실</span><b class="bad">${f(t.loss,3)}</b></div>
    <div class="ln"><span>순손익 (청산 확정)</span><b class="${cls(t.net)}">${f(t.net,3)}</b></div>
    <div class="ln"><span>보유 중 평가손익</span><b class="${cls(t.upnl)}">${f(t.upnl,3)}</b></div>
    <div class="ln"><span>확정 + 평가</span><b class="${cls((t.net||0)+(t.upnl||0))}">${f((t.net||0)+(t.upnl||0),3)}</b></div></div>`;
  h+=`<div class="row" style="margin-top:10px"><button class="stop" onclick="act('stop','${b.name}')">정지</button><button class="go" onclick="act('resume','${b.name}')">재개</button><button class="close" onclick="act('closeall','${b.name}')">전체 청산</button>${b.enabled?`<button onclick="editBudget('${b.name}')">💰 예산 수정</button>`:''}${b.mode=='live'&&lastWd&&lastWd.enabled?`<button onclick="wdOpen()">💸 출금</button>`:''}</div>${b.mode=='live'&&lastWd&&!lastWd.enabled?`<div class="mut sm" style="margin-top:6px">출금: ${esc(lastWd.reason)}</div>`:''}</div>`}
 $('#books').innerHTML=h;
 $('#legs').innerHTML='<tr><th>장부</th><th>전략</th><th>코인</th><th>상태</th><th>지정가</th><th>체결가</th><th>수량</th><th>L</th><th>손절</th><th>익절</th><th>체결</th><th>청산 예정</th></tr>'+d.legs.map(l=>`<tr class="click${l.id==curLeg?' sel':''}" onclick="showChart(${l.id})"><td>${l.book}</td><td>${l.strategy}${l.leg}</td><td>${l.symbol}</td><td>${l.status}</td><td>${l.limit_px}</td><td>${l.fill_px?(+l.fill_px).toPrecision(6):'-'}</td><td>${l.fill_qty||l.plan_qty}</td><td>${l.lev}</td><td>${l.sl_px}</td><td>${l.tp_px||'-'}</td><td>${l.fill_ts_s}</td><td>${l.exit_due_ts_s||l.limit_until_s}</td></tr>`).join('');
 $('#dec').innerHTML='<tr><th>시각</th><th>장부</th><th>코인</th><th>전략</th><th>결정</th><th>이유</th><th>K1/변동</th></tr>'+d.decisions.map(x=>`<tr><td>${x.ts_s}</td><td>${x.book}</td><td>${x.symbol}</td><td>${x.strategy}</td><td class="${x.decision=='enter'?'good':'mut'}">${x.decision=='enter'?'진입':'건너뜀'}</td><td>${esc(x.reason)}</td><td>${x.k1??'-'}/${x.k7??'-'}</td></tr>`).join('');
 $('#closed').innerHTML='<tr><th>청산</th><th>장부</th><th>전략</th><th>코인</th><th>이유</th><th>실전%</th><th>사이트%</th><th>USDT</th><th>보유h</th></tr>'+d.closed.map(l=>`<tr class="click${l.id==curLeg?' sel':''}" onclick="showChart(${l.id})"><td>${l.exit_ts_s}</td><td>${l.book}</td><td>${l.strategy}${l.leg}</td><td>${l.symbol}</td><td>${l.exit_reason}</td><td class="${cls(l.pnl_pct)}">${f(l.pnl_pct)}</td><td>${f(l.site_result)}</td><td class="${cls(l.pnl_usdt)}">${f(l.pnl_usdt,3)}</td><td>${f(l.hold_h,1)}</td></tr>`).join('');
 $('#evs').innerHTML=d.events.map(e=>`<tr><td>${e.ts_s}</td><td class="${e.level=='ERROR'?'bad':'warn'}">${e.kind}</td><td>${esc(e.book)}</td><td style="white-space:normal">${esc(e.msg)}</td></tr>`).join('')||'<tr><td class="mut">없음</td></tr>';
 if(!curLeg){const first=d.legs[0]||d.closed[0];if(first){curLeg=first.id;drawChart(true)}}
 drawEq();
 const t=d.telegram;$('#tg').innerHTML='텔레그램: '+(!t.enabled?'꺼짐(토큰 없음)':t.chat_set?'연결됨':'chat id 미등록'+(t.seen_chat?` — /start 보낸 chat id: <b>${t.seen_chat}</b> (.env 의 TELEGRAM_CHAT_ID 에 넣고 재시작)`:' — 봇에게 /start 를 보내 주세요'))+(t.error?' · 오류: '+esc(t.error):'');}
let pend=null;async function act(cmd,book){const r=await fetch('/api/confirm',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({cmd,book})});const j=await r.json();pend=j.nonce;
 $('#dlgt').textContent=`[${book}] ${cmd=='stop'?'정지 (신규 진입 중단·대기 주문 취소)':cmd=='resume'?'재개':'전체 청산 (보유 전부 시장가)'} — 정말 실행할까요?`;$('#dlg').style.display='flex'}
$('#no').onclick=()=>{pend=null;$('#dlg').style.display='none'};
$('#ok').onclick=async()=>{const r=await fetch('/api/action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({nonce:pend})});const j=await r.json();$('#dlg').style.display='none';alert(j.msg||j.detail);load()};
let ch=null,cs=null,plines=[],curLeg=null,curIv='15',eqc=null,eqs={};
const LW=()=>window.LightweightCharts;
function mk(el){return LW().createChart(el,{autoSize:true,layout:{background:{color:'#161b22'},textColor:'#8b949e'},
 grid:{vertLines:{color:'#21262d'},horzLines:{color:'#21262d'}},timeScale:{timeVisible:true,secondsVisible:false,borderColor:'#30363d'},rightPriceScale:{borderColor:'#30363d'}})}
async function drawChart(fit){if(!curLeg)return;if(!LW()){$('#ct').textContent='차트 라이브러리를 못 불러왔습니다 (인터넷 확인)';return}
 const r=await fetch(`/api/chart?leg_id=${curLeg}&iv=${curIv}`);if(!r.ok)return;const d=await r.json();
 if(!ch){ch=mk($('#chart'));cs=ch.addCandlestickSeries({upColor:'#3fb950',downColor:'#f85149',borderVisible:false,wickUpColor:'#3fb950',wickDownColor:'#f85149'})}
 const px=(d.candles.at(-1)||{}).close||1;const p=Math.min(8,Math.max(2,Math.ceil(-Math.log10(px))+4));
 cs.applyOptions({priceFormat:{type:'price',precision:p,minMove:Math.pow(10,-p)}});
 cs.setData(d.candles);plines.forEach(x=>cs.removePriceLine(x));
 plines=d.lines.map(x=>cs.createPriceLine({price:x.price,color:x.color,lineWidth:1,lineStyle:2,axisLabelVisible:true,title:x.title}));
 cs.setMarkers(d.markers);$('#ct').textContent='차트 — '+d.title+(d.live?' · 15초마다 갱신':'');if(fit)ch.timeScale().fitContent()}
function showChart(id){curLeg=id;drawChart(true);document.querySelectorAll('.tbl tr.sel').forEach(x=>x.classList.remove('sel'));
 window.scrollTo({top:$('#chartcard').offsetTop-60,behavior:'smooth'})}
function setIv(v){curIv=v;document.querySelectorAll('.ivs button').forEach(b=>b.classList.toggle('on',b.dataset.iv==v));drawChart(true)}
async function drawEq(){if(!LW())return;const r=await fetch('/api/equity');if(!r.ok)return;const d=await r.json();
 const col=['#58a6ff','#3fb950','#d29922','#f778ba','#a5d6ff','#ffa657'];if(!eqc)eqc=mk($('#eq'));
 Object.entries(d).forEach(([k,pts],i)=>{if(!eqs[k])eqs[k]=eqc.addLineSeries({color:col[i%col.length],lineWidth:2,title:k,priceLineVisible:false});eqs[k].setData(pts)});
 eqc.timeScale().fitContent();$('#eqnote').textContent=Object.keys(d).length?'':' — 아직 청산된 거래가 없습니다'}
let bnonce=null,bbook=null;
function editBudget(book){const b=lastSnap.books.find(x=>x.name==book);bbook=book;
 let h=`<p><b>[${esc(book)}] 예산 수정</b><br><span class="mut sm">USDT · 0 = 그 종류 끄기 · 올릴 때는 비밀번호를 한 번 더 넣습니다</span></p>`;
 for(const [k,v] of Object.entries(b.strategies)){h+=`<label class="ln"><span>${v.rank} ${k} <span class="mut sm">(최소 ${v.min_budget})</span></span><input type="number" inputmode="decimal" min="0" step="1" id="bud_${k}" value="${v.raw_budget??v.budget}" style="width:120px"></label>`}
 h+=`<div class="row" style="margin-top:14px"><button class="go" onclick="budgetNext()">다음</button><button onclick="closeB()">취소</button></div>`;
 $('#bbox').innerHTML=h;$('#bdlg').style.display='flex'}
async function budgetNext(){const b=lastSnap.books.find(x=>x.name==bbook);const budgets={};
 for(const k of Object.keys(b.strategies)){budgets[k]=+$('#bud_'+k).value}
 const r=await fetch('/api/budget/confirm',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({book:bbook,budgets})});const j=await r.json();
 if(!r.ok){alert(j.detail);return}bnonce=j.nonce;
 let h=`<p><b>[${esc(bbook)}] 이렇게 바꿀까요?</b></p>`+j.diff.map(x=>`<div class="ln"><span>${x.s}</span><b>${x.old} → ${x.new}</b></div>`).join('');
 if(j.needs_password)h+=`<p class="warn sm" style="margin-top:10px">금액을 올리므로 화면 비밀번호를 한 번 더 넣어 주세요</p><input type="password" id="bpw" autocomplete="current-password" style="width:100%">`;
 h+=`<div class="row" style="margin-top:14px"><button class="close" onclick="budgetApply()">적용</button><button onclick="closeB()">취소</button></div>`;$('#bbox').innerHTML=h}
async function budgetApply(){const pw=$('#bpw')?$('#bpw').value:'';
 const r=await fetch('/api/budget/apply',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({nonce:bnonce,password:pw})});const j=await r.json();
 closeB();alert(j.msg||j.detail);setTimeout(load,2500)}
function closeB(){bnonce=null;wnonce=null;$('#bdlg').style.display='none'}
let wnonce=null;
function wdOpen(){const w=lastWd||{};
 $('#bbox').innerHTML=`<p><b>💸 출금 → ${esc(w.dest)}</b><br><span class="mut sm">달러 금액 · 통장에 이 금액 이상 들어오게 계산 · 1회 최대 ${w.max_usd}달러 · 오늘 남은 ${w.left_today}/${w.count_per_day}회</span></p>
 <label class="ln"><span>금액 (달러)</span><input type="number" inputmode="decimal" min="1" step="1" id="wd_usd" value="10" style="width:120px"></label>
 <div class="row" style="margin-top:14px"><button class="go" onclick="wdNext()">계산</button><button onclick="closeB()">취소</button></div>`;$('#bdlg').style.display='flex'}
async function jget(r){try{return await r.json()}catch(e){return {detail:'응답을 읽지 못했습니다 (HTTP '+r.status+')'}}}
async function wdNext(){const usd=+$('#wd_usd').value;$('#bbox').innerHTML='<p class="mut">계산 중…</p>';
 let r,j;try{r=await fetch('/api/withdraw/preview',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({usd})});j=await jget(r)}
 catch(e){closeB();alert('연결 오류 — 다시 해 주세요');return}
 if(!r.ok){closeB();alert(j.detail);return}wnonce=j.nonce;const p=j.plan;const n=x=>Math.round(x).toLocaleString();
 $('#bbox').innerHTML=`<p><b>이렇게 출금할까요?</b></p>
 <div class="ln"><span>받는 곳</span><b>${esc(p.dest)}</b></div>
 <div class="ln"><span>바이비트에서 보낼 XRP</span><b>${p.amount}</b></div>
 <div class="ln mut sm"><span>바이비트 출금 수수료 ${p.wd_fee} XRP 포함</span></div>
 <div class="ln"><span>부족분 현물 매수</span><b>${+p.buy>0?`XRP ${p.buy} (약 ${f(p.buy_usdt)} USDT)`:'없음'}</b></div>
 <div class="ln"><span>목표 (${p.usd}달러)</span><b>${n(p.target_krw)}원</b></div>
 <div class="ln"><span>통장 예상 (지금 시세)</span><b class="good">${n(p.bank_krw)}원</b></div>
 <div class="mut sm">업비트 XRP ${n(p.xrp_krw)}원 · 원/달러 ${n(p.usd_krw)} · 여유 ${p.buffer_pct}% · 오늘 남은 ${p.left_today}회</div>`
  +(p.warnings||[]).map(x=>`<p class="warn sm">⚠ ${esc(x)}</p>`).join('')
  +`<p class="warn sm" style="margin-top:10px">화면 비밀번호를 한 번 더 넣어 주세요 (2분 안에)</p><input type="password" id="wpw" autocomplete="current-password" style="width:100%">
 <div class="row" style="margin-top:14px"><button class="close" onclick="wdApply()">출금</button><button onclick="closeB()">취소</button></div>`}
async function wdApply(){const pw=$('#wpw').value;const nn=wnonce;$('#bbox').innerHTML='<p class="mut">출금 처리 중… (최대 20초)</p>';
 let msg;try{const r=await fetch('/api/withdraw/apply',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({nonce:nn,password:pw})});const j=await jget(r);msg=j.msg||j.detail}
 catch(e){msg='연결이 끊겨 결과를 모릅니다 — 다시 누르지 말고 텔레그램과 바이비트 앱 출금 내역을 먼저 확인하세요'}
 closeB();alert(msg);load()}
load();setInterval(load,10000);setInterval(()=>drawChart(false),15000);
</script></body></html>"""
