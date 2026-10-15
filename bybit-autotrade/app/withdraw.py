"""
출금 — 바이비트 → 업비트 XRP 주소 1개 (사용자 결정 2026-10-15, NOTES 절대 규칙의 예외).
- 받는 곳(코인·체인·주소·태그)은 config.yaml 의 withdraw 블록에만 있다. 웹·텔레그램에서 바꿀 수 없고, REST 단
  (bybit_rest.WITHDRAW_ALLOWED + withdraw_dest)에서도 다른 주소로의 출금을 거부한다. 바이비트 화이트리스트가 마지막 관문.
- 요청은 달러. 통장에 들어올 원화 = 도착 XRP × 업비트 시세 × (1 − 업비트 수수료) − 업비트 원화 출금 수수료 가
  '요청 달러 × 원/달러' 이상이 되도록 XRP 수량을 잡는다 (여유 buffer_pct). 조금 많은 것은 괜찮고, 모자라면 안 된다.
- 순서: 시세·수수료 조회 → 계산 → (부족하면) 바이비트 현물 XRPUSDT 시장가 매수 → 잔고 확인 → 기록(보냄) → 출금.
- 하루(KST) count_per_day 회, 1회 max_usd 달러까지. 매매 엔진과 따로 돌고(자기 REST 세션, 재시도 없음), 한 번에 하나만.
- 출금 설정이 잘못돼도 출금만 꺼지고 매매는 그대로 돈다 (check_config 가 이유를 돌려줌).
- ⚠ 2026-10-15 작성 때 바이비트 V5 문서를 확인하지 못했다 (클라우드에서 접속 막힘). "V5 확인" 표시가 붙은 경로·파라미터·
  응답 칸은 공식 문서로 확인한 뒤 config 의 withdraw.enabled 를 true 로 바꾼다 (README 출금 절).
"""
import time
import hashlib
import logging
import threading
from decimal import Decimal, ROUND_UP
import requests
from .timeutil import now_ms, kst_day

log = logging.getLogger("withdraw")

UPBIT_TICKER = "https://api.upbit.com/v1/ticker"
BYBIT_PUBLIC = "https://api.bybit.com"
ACCOUNT_TYPE = "UTA"          # V5 확인: 통합계정 잔고에서 바로 출금할 때의 accountType 값
XRP_ALPHABET = "rpshnaf39wBUDNEGHJKLM4PQRST7VWXYZ2bcdeCg65jkm8oFqi1tuvAxyz"
COUNTED = ("pending", "ok", "unknown")      # 하루 횟수에 세는 상태 (보냈거나 보냈을 수 있음)


def definite_reject(code) -> bool:
    """출금 요청이 확실히 거절된 오류인지 (V5 확인). 그 밖의 코드는 '결과 모름' 으로 본다 (다시 보내면 두 번 나갈 수 있음)."""
    try:
        c = int(code)
    except (TypeError, ValueError):
        return False
    return c in (10001, 10003, 10004, 10005, 10010) or 131000 <= c < 132000


class WithdrawError(Exception):
    pass


class _Done(WithdrawError):
    """이미 기록·알림을 마친 출금 오류"""


def valid_xrp_address(a: str) -> bool:
    """XRP 클래식 주소 체크섬 (base58, 이중 SHA-256 앞 4바이트)"""
    try:
        n = 0
        for ch in a:
            n = n * 58 + XRP_ALPHABET.index(ch)
        raw = n.to_bytes(25, "big")
    except (ValueError, OverflowError, TypeError):
        return False
    body, cs = raw[:-4], raw[-4:]
    return a.startswith("r") and body[0] == 0 and hashlib.sha256(hashlib.sha256(body).digest()).digest()[:4] == cs


def check_config(w: dict):
    """출금 설정 검사·정리. (정리된 설정, None) 또는 (None, 이유). 출금만 꺼지고 매매는 그대로."""
    if w.get("enabled") is not True:
        return None, "꺼짐 (설정 withdraw.enabled — 바이비트 V5 문서 확인 뒤 true)"
    if w.get("coin") != "XRP" or not isinstance(w.get("chain"), str) or not w.get("chain"):
        return None, "coin 은 XRP, chain 필요"
    if not isinstance(w.get("address"), str) or not valid_xrp_address(w["address"]):
        return None, "주소가 비었거나 XRP 주소 체크섬이 맞지 않음"
    tag = w.get("tag")
    if not isinstance(tag, str) or not tag.isdigit() or int(tag) > 4294967295:
        return None, "데스티네이션 태그는 따옴표 안의 숫자 (예: tag: \"123\")"
    out = dict(w)
    try:
        for k in ("max_usd", "buffer_pct", "upbit_fee_pct", "upbit_krw_withdraw_fee", "spot_fee_pct", "usd_krw", "upbit_min_krw"):
            v = w.get(k)
            if isinstance(v, bool) or v is None or isinstance(v, str):
                raise ValueError(k)
            out[k] = float(v)
        n = w.get("count_per_day")
        if isinstance(n, bool) or not isinstance(n, int):
            raise ValueError("count_per_day")
    except (TypeError, ValueError) as e:
        return None, f"{e} 는 따옴표·쉼표 없는 숫자여야 함"
    if not out["max_usd"] > 0:
        return None, "max_usd 는 0보다 커야 함"
    if not 1 <= n <= 5:
        return None, "count_per_day 는 1~5 정수 (사용자 한도 5회)"
    if not 0.5 <= out["buffer_pct"] <= 20:
        return None, "buffer_pct 는 0.5~20 (받는 돈이 모자라지 않게)"
    for k in ("upbit_fee_pct", "upbit_krw_withdraw_fee", "spot_fee_pct", "usd_krw", "upbit_min_krw"):
        if out[k] < 0:
            return None, f"{k} 는 0 이상"
    return out, None


def dec_up(x, step) -> Decimal:
    """x 를 step 단위로 올림 (Decimal, 지수 표기 없이 쓸 수 있는 자리수)"""
    s = Decimal(str(step)).normalize()
    return (Decimal(str(x)) / s).to_integral_value(rounding=ROUND_UP) * s


def fmt(d: Decimal) -> str:
    return format(d, "f")


def compute(usd: float, q: dict, w: dict) -> dict:
    """보낼 XRP 수량 계산 (순수 함수). q = 시세·수수료·잔고, w = 설정.
    도착 = 출금액 − 바이비트 수수료 로 보고(적게 도착하는 쪽), 잔고는 수수료가 따로 빠지는 경우까지 대비해 출금액 + 수수료를 준비."""
    up_fee, krw_fee, buf = w["upbit_fee_pct"] / 100, float(w["upbit_krw_withdraw_fee"]), w["buffer_pct"] / 100
    target = usd * q["usd_krw"]
    arrive_need = (target + krw_fee) / (q["xrp_krw"] * (1 - up_fee)) * (1 + buf)
    amount = dec_up(max(arrive_need + q["wd_fee"], q["wd_min"]), q["wd_step"])
    need_bal = amount + Decimal(str(q["wd_fee"]))
    short = need_bal - Decimal(str(q["have_xrp"]))
    buy = Decimal(0)
    if short > 0:
        buy = max(dec_up(float(short) / (1 - w["spot_fee_pct"] / 100), q["spot_step"]),
                  dec_up(q["spot_min_qty"], q["spot_step"]),
                  dec_up(q["spot_min_amt"] / q["xrp_usdt"], q["spot_step"]))
    arrive = float(amount) - q["wd_fee"]
    bank = arrive * q["xrp_krw"] * (1 - up_fee) - krw_fee
    return {"usd": usd, "usd_krw": q["usd_krw"], "xrp_krw": q["xrp_krw"], "xrp_usdt": q["xrp_usdt"], "target_krw": target,
            "amount": fmt(amount), "wd_fee": q["wd_fee"], "wd_min": q["wd_min"], "min_bound": q["wd_min"] > arrive_need + q["wd_fee"],
            "need_bal": fmt(need_bal), "have_xrp": q["have_xrp"], "buy": fmt(buy), "buy_usdt": float(buy) * q["xrp_usdt"],
            "avail_usd": q.get("avail_usd"), "arrive": arrive, "bank_krw": bank, "bank_usd": bank / q["usd_krw"],
            "buffer_pct": w["buffer_pct"]}


class Withdrawer:
    def __init__(self, w: dict, rest, book, store, notify=None, http=None, clock=now_ms, sleep=time.sleep):
        self.w, self.rest, self.book, self.st = w, rest, book, store
        self.notify = notify or (lambda t: None)
        self.http = http or requests.Session()
        self.clock, self.sleep = clock, sleep
        self.lock = threading.Lock()
        self.dest = {"coin": w["coin"], "chain": w["chain"], "address": w["address"], "tag": w["tag"]}
        self._seq = 0

    # ---------- 기록 ----------
    def log_rows(self):
        return self.st.get("withdraw:log", []) or []

    def used_today(self) -> int:
        """오늘(KST) 출금 요청을 보낸(또는 보냈을 수 있는) 횟수. 확실한 실패는 세지 않음."""
        today = kst_day(self.clock())
        return sum(1 for r in self.log_rows() if kst_day(r["ts"]) == today and r["status"] in COUNTED)

    def status(self) -> dict:
        a = self.dest["address"]
        return {"enabled": True, "max_usd": self.w["max_usd"], "left_today": self.w["count_per_day"] - self.used_today(),
                "count_per_day": self.w["count_per_day"], "dest": f"업비트 XRP {a[:4]}…{a[-4:]} (태그 …{self.dest['tag'][-4:]})"}

    def _record(self, status, p, msg, wid=None, rid=None):
        """기록 한 줄 추가 또는 (rid 가 있으면) 그 줄의 상태 바꿈"""
        rows = self.log_rows()
        row = next((r for r in rows if rid and r.get("rid") == rid), None)
        if row is None:
            self._seq += 1
            rid = rid or f"{self.clock()}-{self._seq}"
            row = {"rid": rid, "ts": self.clock(), "usd": p.get("usd"), "amount": p.get("amount"), "buy": p.get("buy")}
            rows.append(row)
        row.update(status=status, id=wid or row.get("id"), msg=msg[:200])
        if len(rows) > 300:                                    # 오래된 날 기록만 줄임 (오늘 횟수는 그대로)
            today = kst_day(self.clock())
            rows = [r for r in rows if kst_day(r["ts"]) == today or r is row]
        self.st.put("withdraw:log", rows)
        self.st.event("ERROR" if status == "fail" else "WARN", self.book.name, "출금", msg[:500])
        return rid

    # ---------- 시세·수수료 ----------
    def _get(self, url, params):
        r = self.http.get(url, params=params, timeout=10)
        r.raise_for_status()
        return r.json()

    def _bybit_public(self, path, params):
        d = self._get(f"{BYBIT_PUBLIC}{path}", params)
        if d.get("retCode") not in (0, None):
            raise WithdrawError(f"바이비트 공개 시세 오류 {path} retCode={d.get('retCode')}")
        return d["result"]["list"][0]

    def quotes(self) -> dict:
        coin, sym = self.w["coin"], f"{self.w['coin']}USDT"
        fixed = self.w["usd_krw"]
        markets = f"KRW-{coin}" + ("" if fixed else ",KRW-USDT")
        try:
            up = {x["market"]: float(x["trade_price"]) for x in self._get(UPBIT_TICKER, {"markets": markets})}
        except Exception as e:
            raise WithdrawError(f"업비트 시세를 못 받음: {type(e).__name__}")
        usd_krw = fixed or up.get("KRW-USDT")
        if not usd_krw or not up.get(f"KRW-{coin}"):
            raise WithdrawError("원/달러 또는 업비트 XRP 시세가 없음 (설정 usd_krw 로 원/달러를 직접 넣을 수 있음)")
        # V5 확인: /v5/asset/coin/query-info → rows[].chains[] (chain, withdrawFee, withdrawPercentageFee, withdrawMin, minAccuracy, chainWithdraw)
        ci = self.rest.call("GET", "/v5/asset/coin/query-info", {"coin": coin})
        ch = next((c for row in ci.get("rows", []) for c in row.get("chains", []) if c.get("chain") == self.w["chain"]), None)
        if not ch:
            raise WithdrawError(f"바이비트에 {coin} {self.w['chain']} 체인 정보가 없음")
        if str(ch.get("chainWithdraw", "1")) != "1":
            raise WithdrawError(f"바이비트가 지금 {coin} {self.w['chain']} 출금을 막아 둠")
        if float(ch.get("withdrawPercentageFee") or 0) > 0:
            raise WithdrawError("바이비트 출금 수수료에 % 수수료가 있어 계산이 맞지 않음 — 코드 확인 필요")
        acc_digits = int(str(ch.get("minAccuracy") or "6"))
        # V5 확인: wallet-balance (UNIFIED) → list[0].totalAvailableBalance, coin[].walletBalance
        wb = self.rest.call("GET", "/v5/account/wallet-balance", {"accountType": "UNIFIED", "coin": f"USDT,{coin}"})
        acc = (wb.get("list") or [{}])[0]
        have = float(next((c.get("walletBalance") for c in acc.get("coin", []) if c.get("coin") == coin), 0) or 0)
        tab = acc.get("totalAvailableBalance")
        tk = self._bybit_public("/v5/market/tickers", {"category": "spot", "symbol": sym})
        it = self._bybit_public("/v5/market/instruments-info", {"category": "spot", "symbol": sym})
        px = float(tk.get("ask1Price") or 0) or float(tk.get("lastPrice") or 0)
        if px <= 0:
            raise WithdrawError("바이비트 XRP 현물 가격을 못 받음")
        lf = it["lotSizeFilter"]
        return {"usd_krw": float(usd_krw), "xrp_krw": up[f"KRW-{coin}"], "xrp_usdt": px,
                "wd_fee": float(ch["withdrawFee"]), "wd_min": float(ch.get("withdrawMin") or 0),
                "wd_step": fmt(Decimal(1).scaleb(-acc_digits)), "have_xrp": have,
                "avail_usd": float(tab) if tab not in (None, "") else None,
                "spot_step": str(lf.get("basePrecision") or lf.get("qtyStep") or "0.01"),
                "spot_min_qty": float(lf.get("minOrderQty") or 0), "spot_min_amt": float(lf.get("minOrderAmt") or 0)}

    # ---------- 미리 보기 ----------
    def preview(self, usd) -> dict:
        try:
            usd = float(usd)
        except (TypeError, ValueError):
            raise WithdrawError("금액이 숫자가 아님")
        if not 0 < usd <= self.w["max_usd"]:
            raise WithdrawError(f"금액은 0 초과 {self.w['max_usd']:g} 달러 이하")
        if self.used_today() >= self.w["count_per_day"]:
            raise WithdrawError(f"오늘 출금 {self.w['count_per_day']}회를 다 썼습니다 (내일 다시)")
        p = compute(usd, self.quotes(), self.w)
        if p["target_krw"] < self.w["upbit_min_krw"]:
            raise WithdrawError(f"업비트 최소 금액 {self.w['upbit_min_krw']:,.0f}원 때문에 약 "
                                f"{self.w['upbit_min_krw'] / p['usd_krw']:.1f}달러 이상만 됩니다")
        send_usd = float(p["amount"]) * p["xrp_usdt"]
        if send_usd > usd * 1.3 + 5:                      # 수수료를 다 더해도 이보다 클 수 없음
            if p["min_bound"]:
                raise WithdrawError(f"바이비트 최소 출금 수량 {p['wd_min']:g} XRP (약 {p['wd_min'] * p['xrp_usdt']:.1f}달러) "
                                    "때문에 요청보다 훨씬 많이 보내야 함 — 금액을 올려 주세요")
            raise WithdrawError(f"계산 이상: 보낼 XRP 가치 {send_usd:.2f}달러 > 요청 {usd:g}달러 기준 상한")
        if p["avail_usd"] is not None and p["buy_usdt"] > p["avail_usd"] * 0.98:
            raise WithdrawError(f"바이비트 사용 가능 USDT 부족 (필요 약 {p['buy_usdt']:.2f}, 사용 가능 {p['avail_usd']:.2f})")
        warn = []
        eq, tot = self.book.last_equity, sum(self.book.raw_budgets.values())
        if eq is not None and eq - p["buy_usdt"] < tot:
            warn.append(f"출금 뒤 잔고 약 {eq - p['buy_usdt']:.2f} < 예산 합계 {tot:g} — 예산을 낮추는 게 좋습니다")
        p["warnings"] = warn
        p["left_today"] = self.w["count_per_day"] - self.used_today()
        p["dest"] = self.status()["dest"]
        return p

    # ---------- 실행 ----------
    def _ts(self):
        f = getattr(self.rest, "now_ms", None)
        return f() if callable(f) else self.clock()

    def execute(self, prev: dict) -> str:
        if not self.lock.acquire(blocking=False):
            raise WithdrawError("다른 출금이 진행 중입니다")
        p, stage, bought = prev, "계산", False

        def fail(msg, rid=None):
            full = f"출금 안 됨 ({stage}): {msg}" + (" — 매수한 XRP 는 바이비트에 남아 있고 다음 출금 때 씁니다" if bought else "")
            self._safe_record("fail", p, full, rid=rid)
            self.notify("⚠️ " + full)
            return _Done(full)

        try:
            try:
                p = self.preview(prev["usd"])
            except WithdrawError as e:
                raise fail(str(e))
            if Decimal(p["amount"]) > Decimal(prev["amount"]) * Decimal("1.03"):
                raise fail("시세가 움직여 보낼 수량이 3% 넘게 늘었습니다 — 다시 계산해 주세요")
            coin = self.w["coin"]
            if Decimal(p["buy"]) > 0:
                stage = "현물 매수"
                try:
                    # V5 확인: 현물 시장가를 코인 수량으로 사려면 marketUnit=baseCoin, 빌리지 않음 isLeverage=0
                    self.rest.call("POST", "/v5/order/create", {"category": "spot", "symbol": f"{coin}USDT", "side": "Buy",
                                                                "orderType": "Market", "qty": p["buy"], "marketUnit": "baseCoin",
                                                                "isLeverage": 0, "orderLinkId": f"ATW-{self.clock()}"})
                except Exception as e:
                    if getattr(e, "code", None) is None and not isinstance(e, PermissionError):
                        raise fail(f"매수 결과 모름 ({type(e).__name__}) — 바이비트 앱에서 XRP 매수 여부를 확인하세요 "
                                   "(샀다면 다음 출금 때 그 XRP 를 씁니다)")
                    raise fail(str(e)[:200])
                bought = True
                stage = "매수 잔고 확인"
                try:
                    self._wait_balance(Decimal(p["need_bal"]))
                except WithdrawError as e:
                    raise fail(str(e))
            stage = "출금"
            rid = self._record("pending", p, f"출금 요청 보냄: XRP {p['amount']} → {p['dest']}")   # 보내기 전에 먼저 기록
            try:
                # V5 확인: withdraw/create 파라미터 (coin, chain, address, tag, amount, timestamp, forceChain, accountType)
                res = self.rest.call("POST", "/v5/asset/withdraw/create", {**self.dest, "amount": p["amount"],
                                                                          "timestamp": self._ts(), "forceChain": 1,
                                                                          "accountType": ACCOUNT_TYPE})
            except PermissionError as e:                               # 코드 안 관문에서 막음 → 보내지 않음
                raise fail(str(e), rid)
            except Exception as e:
                if definite_reject(getattr(e, "code", None)):
                    raise fail(str(e)[:200], rid)
                msg = (f"⚠️ 출금 결과 모름 ({str(e)[:120] or type(e).__name__}) — 접수됐을 수 있습니다. 바이비트 앱 출금 내역을 "
                       f"확인하세요. XRP {p['amount']} (하루 횟수에는 셈, 다시 누르기 전에 꼭 확인)")
                self._safe_record("unknown", p, msg, rid=rid)
                self._after_send(p)
                self.notify(msg)
                raise _Done(msg)
            wid = res.get("id") if isinstance(res, dict) else None
            msg = (f"💸 출금 접수: XRP {p['amount']} → {p['dest']} · 바이비트 출금 id {wid}\n"
                   f"요청 {p['usd']:g}달러 · 업비트에서 팔면 통장 예상 약 {p['bank_krw']:,.0f}원 "
                   f"(목표 {p['target_krw']:,.0f}원, 여유 {p['buffer_pct']:g}%)"
                   + (f"\n현물 매수 XRP {p['buy']} (약 {p['buy_usdt']:.2f} USDT)" if bought else ""))
            self.notify(msg)                                           # 접수된 뒤에는 알림 먼저 — 그 뒤 기록이 실패해도 '안 됨' 이 아님
            self._safe_record("ok", p, msg, wid=wid, rid=rid)
            self._after_send(p)
            log.info("출금 접수 id=%s XRP %s", wid, p["amount"])
            return msg
        except _Done:
            raise
        except Exception as e:                                         # 보내기 전의 예상 못 한 오류
            raise fail(f"{type(e).__name__}: {str(e)[:200]}")
        finally:
            self.lock.release()

    def _safe_record(self, *a, **k):
        try:
            self._record(*a, **k)
        except Exception as e:
            log.error("출금 기록 실패: %s", e)

    def _after_send(self, p):
        """보낸(또는 보냈을 수 있는) 뒤: 잔고 추정을 줄여 예산 올리기 검사가 옛 잔고를 쓰지 않게"""
        try:
            if self.book.last_equity is not None and p.get("buy_usdt"):
                self.book.last_equity -= p["buy_usdt"]
        except Exception:
            pass

    def _wait_balance(self, need: Decimal, tries=10):
        coin = self.w["coin"]
        for _ in range(tries):
            try:
                wb = self.rest.call("GET", "/v5/account/wallet-balance", {"accountType": "UNIFIED", "coin": coin})
                acc = (wb.get("list") or [{}])[0]
                have = Decimal(str(next((c.get("walletBalance") for c in acc.get("coin", []) if c.get("coin") == coin), 0) or 0))
                if have >= need:
                    return
            except Exception as e:
                log.warning("잔고 확인 실패 (다시 봄): %s", e)
            self.sleep(1)
        raise WithdrawError(f"매수한 {coin} 가 아직 잔고에 안 보임 (필요 {need}) — 출금 안 함. 매수한 XRP 는 바이비트에 남아 있습니다")


def make_withdrawer(cfg, books, store, notify, secret):
    """설정·live 장부 상태를 보고 출금기를 만든다. (출금기 또는 None, 상태 설명). 어떤 경우에도 예외를 밖으로 내지 않는다."""
    try:
        from .bybit_rest import BybitREST, WITHDRAW_ALLOWED
        w, why = check_config(cfg.get("withdraw") or {})
        if why:
            return None, why if why.startswith("꺼짐") else f"꺼짐: {why}"
        live = next((b for b in books if b.mode == "live" and b.enabled), None)
        if not live:
            return None, "꺼짐: live 장부가 꺼져 있음"
        k, s = secret("BYBIT_LIVE_API_KEY"), secret("BYBIT_LIVE_API_SECRET")
        dest = {"coin": w["coin"], "chain": w["chain"], "address": w["address"], "tag": w["tag"]}
        rest = BybitREST("live", k, s, retries=1, allowed=WITHDRAW_ALLOWED, withdraw_dest=dest)
        rest.sync_time()
        return (Withdrawer(w, rest, live, store, notify),
                f"켜짐 → 업비트 XRP {w['address'][:4]}…{w['address'][-4:]} · 하루 {w['count_per_day']}회 · 1회 {w['max_usd']:g}달러")
    except Exception as e:
        log.error("출금기 만들기 실패: %s", e)
        return None, f"꺼짐: 설정 오류 {type(e).__name__}"
