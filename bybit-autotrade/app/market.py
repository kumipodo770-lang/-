"""
공개 시세 (서명 없음, 메인넷). demo 의 공개 데이터는 메인넷과 같다 (문서 Demo Trading Service).
instruments-info 는 하루 한 번 캐시, kline 은 1분·1시간, funding 이력.
"""
import time
import logging
import requests

log = logging.getLogger("market")


class Market:
    def __init__(self, base="https://api.bybit.com", timeout=10, retries=3):
        self.base = base
        self.timeout = timeout
        self.retries = retries
        self.s = requests.Session()
        self._inst = {}
        self._inst_ts = 0

    def _get(self, path, params):
        last = None
        for a in range(self.retries):
            try:
                r = self.s.get(self.base + path, params=params, timeout=self.timeout)
                if r.status_code == 403:
                    raise RuntimeError(f"{path} HTTP 403 (IP 요청 과다)")
                d = r.json()
                if d.get("retCode") == 0:
                    return d["result"]
                last = RuntimeError(f"{path} retCode={d.get('retCode')} {d.get('retMsg')}")
                if d.get("retCode") not in (10006, 10016):
                    raise last
            except (requests.RequestException, ValueError) as e:
                last = RuntimeError(f"{path} {type(e).__name__}")
            time.sleep(2 ** a)
        raise last

    # ---- 종목 규칙 ----
    def instruments(self, force=False) -> dict:
        if self._inst and not force and time.time() - self._inst_ts < 86400:
            return self._inst
        out, cursor = {}, None
        for _ in range(20):                       # 쪽 수 상한
            res = self._get("/v5/market/instruments-info", {"category": "linear", "limit": 1000, "cursor": cursor})
            for it in res.get("list", []):
                if it.get("quoteCoin") != "USDT" or it.get("contractType") != "LinearPerpetual":
                    continue
                lf, pf, lv = it["lotSizeFilter"], it["priceFilter"], it["leverageFilter"]
                out[it["symbol"]] = {
                    "status": it.get("status"),
                    "tick": float(pf["tickSize"]),
                    "min_qty": float(lf["minOrderQty"]),
                    "qty_step": float(lf["qtyStep"]),
                    "max_mkt_qty": float(lf.get("maxMktOrderQty") or lf["maxOrderQty"]),
                    "min_notional": float(lf.get("minNotionalValue") or 5),
                    "max_lev": float(lv["maxLeverage"]),
                    "funding_min": int(it.get("fundingInterval") or 480),
                }
            cursor = res.get("nextPageCursor")
            if not cursor:
                break
        self._inst, self._inst_ts = out, time.time()
        return out

    def instrument(self, symbol):
        return self.instruments().get(symbol)

    # ---- 가격 ----
    def ticker(self, symbol) -> dict:
        res = self._get("/v5/market/tickers", {"category": "linear", "symbol": symbol})
        t = res["list"][0]
        return {"last": float(t["lastPrice"]), "mark": float(t["markPrice"]), "funding": float(t.get("fundingRate") or 0),
                "next_funding": int(t.get("nextFundingTime") or 0)}

    def all_tickers(self) -> dict:
        """USDT 무기한 전 종목 현재가 한 번에 {symbol: last} (10초 캐시, 화면용 — 별도 세션)"""
        import threading
        if not hasattr(self, "_tk_lock"):
            self._tk_lock, self._tk_s, self._tk = threading.Lock(), requests.Session(), (0, {})
        with self._tk_lock:
            if time.time() - self._tk[0] < 10:
                return self._tk[1]
            r = self._tk_s.get(self.base + "/v5/market/tickers", params={"category": "linear"}, timeout=self.timeout).json()
            if r.get("retCode") != 0:
                raise RuntimeError(f"tickers retCode={r.get('retCode')}")
            out = {t["symbol"]: float(t["lastPrice"]) for t in r["result"]["list"] if t.get("lastPrice")}
            self._tk = (time.time(), out)
            return out

    def klines(self, symbol, interval="1", start=None, end=None, limit=1000):
        """오름차순 [(start_ms, o, h, l, c)] — 마지막 봉은 진행 중일 수 있음"""
        res = self._get("/v5/market/kline", {"category": "linear", "symbol": symbol, "interval": interval,
                                             "start": start, "end": end, "limit": limit})
        rows = [(int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4])) for x in res.get("list", [])]
        return sorted(rows)

    def funding_history(self, symbol, start=None, end=None, limit=50):
        res = self._get("/v5/market/funding/history", {"category": "linear", "symbol": symbol,
                                                       "startTime": start, "endTime": end, "limit": limit})
        return sorted((int(x["fundingRateTimestamp"]), float(x["fundingRate"])) for x in res.get("list", []))

    def mmr(self, symbol) -> float | None:
        """가장 낮은 위험 단계의 유지증거금률(소수). 단위가 문서에 없어 값이 1 이상이면 % 로 보고 나눔."""
        try:
            res = self._get("/v5/market/risk-limit", {"category": "linear", "symbol": symbol})
            tiers = res.get("list", [])
            if not tiers:
                return None
            low = min(tiers, key=lambda t: float(t["riskLimitValue"]))
            v = float(low["maintenanceMargin"])
            return v / 100 if v >= 0.5 else v
        except Exception as e:
            log.warning("mmr 조회 실패 %s: %s", symbol, e)
            return None
