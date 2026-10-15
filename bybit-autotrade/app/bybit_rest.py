"""
바이비트 V5 REST (서명). 문서: https://bybit-exchange.github.io/docs/v5/guide
- 서명 = HMAC_SHA256(timestamp + apiKey + recvWindow + (GET: queryString | POST: jsonBody)) 소문자 hex
- 허용한 엔드포인트만 부를 수 있다 (ALLOWED). 매매용 목록에는 출금·자산 이동(asset 계열) 엔드포인트가 없다.
- 출금 기능(사용자 결정 2026-10-15)만 따로 WITHDRAW_ALLOWED 로 만든 클라이언트를 쓴다. 그 클라이언트는 출금 요청의
  코인·체인·주소·태그가 설정 파일의 화이트리스트 주소(withdraw_dest)와 다르면 보내지 않고, 주문은 현물 매수만 낸다.
- 키·서명·헤더는 로그·예외 메시지에 넣지 않는다.
- 재시도: 네트워크 오류·5xx·10006(요청 과다)·10002(시각) 만, 최대 retries 번. 업무 오류(retCode≠0)는 재시도 안 함.
"""
import hmac
import json
import time
import hashlib
import logging
from urllib.parse import urlencode
import requests

log = logging.getLogger("bybit")

BASES = {"demo": "https://api-demo.bybit.com", "live": "https://api.bybit.com"}
ALLOWED = {
    ("GET", "/v5/account/info"),
    ("GET", "/v5/account/wallet-balance"),
    ("GET", "/v5/order/realtime"),
    ("GET", "/v5/order/history"),
    ("POST", "/v5/order/create"),
    ("POST", "/v5/order/amend"),
    ("POST", "/v5/order/cancel"),
    ("GET", "/v5/position/list"),
    ("POST", "/v5/position/set-leverage"),
    ("GET", "/v5/position/closed-pnl"),
    ("GET", "/v5/execution/list"),
    ("POST", "/v5/account/demo-apply-money"),  # demo 전용 (아래에서 막음)
}
# 출금 전용 (app/withdraw.py). ⚠ V5 문서 확인 전 (2026-10-15) — 경로·파라미터는 문서로 확인한 뒤 출금을 켠다.
WITHDRAW_ALLOWED = {
    ("GET", "/v5/asset/coin/query-info"),       # 출금 수수료·최소 수량·소수 자리
    ("GET", "/v5/account/wallet-balance"),      # USDT 사용 가능액·XRP 잔고
    ("POST", "/v5/order/create"),               # 현물 XRPUSDT 시장가 매수만 (아래에서 막음)
    ("POST", "/v5/asset/withdraw/create"),      # 화이트리스트 주소로만 (아래에서 막음)
}
RETRY_CODES = {10006, 10002, 10016}


class BybitError(Exception):
    def __init__(self, code, msg, path):
        super().__init__(f"{path} retCode={code} {msg}")
        self.code, self.msg, self.path = code, msg, path


class BybitNetError(Exception):
    pass


class BybitREST:
    def __init__(self, mode: str, api_key: str, api_secret: str, retries: int = 3, recv_window: int = 10000, timeout: int = 10,
                 allowed=None, withdraw_dest=None):
        if mode not in BASES:
            raise ValueError("mode 는 demo 또는 live")
        self.mode = mode
        self.allowed = allowed or ALLOWED
        self.withdraw_dest = withdraw_dest    # {"coin","chain","address","tag"} — 출금 클라이언트만
        self.base = BASES[mode]
        self._key = api_key
        self._secret = api_secret.encode()
        self.retries = retries
        self.recv = str(recv_window)
        self.timeout = timeout
        self.s = requests.Session()
        self.time_offset = 0

    def __repr__(self):
        return f"BybitREST(mode={self.mode})"   # 키는 절대 안 보임

    def sync_time(self):
        try:
            r = self.s.get("https://api.bybit.com/v5/market/time", timeout=self.timeout).json()
            self.time_offset = int(r["time"]) - int(time.time() * 1000)
        except Exception as e:  # 시간 동기화 실패는 치명적이지 않음
            log.warning("server time sync 실패: %s", type(e).__name__)

    def now_ms(self) -> int:
        """서버 시각에 맞춘 현재 ms (서명 헤더와 같은 기준)"""
        return int(time.time() * 1000) + self.time_offset

    def _sign(self, ts: str, payload: str) -> str:
        return hmac.new(self._secret, (ts + self._key + self.recv + payload).encode(), hashlib.sha256).hexdigest()

    def call(self, method: str, path: str, params: dict | None = None) -> dict:
        if (method, path) not in self.allowed:
            raise PermissionError(f"허용 안 된 엔드포인트 {method} {path}")
        if path == "/v5/asset/withdraw/create":
            d = self.withdraw_dest
            if not d or any(str((params or {}).get(k, "")) != str(d[k]) for k in ("coin", "chain", "address", "tag")):
                raise PermissionError("출금은 설정 파일의 화이트리스트 주소로만")
        if self.withdraw_dest and path == "/v5/order/create":
            p = params or {}
            if (p.get("category") != "spot" or p.get("side") != "Buy" or p.get("orderType") != "Market"
                    or p.get("marketUnit") != "baseCoin" or p.get("isLeverage") not in (None, 0, "0")
                    or p.get("symbol") != f"{self.withdraw_dest['coin']}USDT"):
                raise PermissionError("출금 클라이언트는 출금할 코인의 현물 시장가 매수(빌리지 않음)만")
        if path == "/v5/account/demo-apply-money" and self.mode != "demo":
            raise PermissionError("demo 자금 신청은 demo 에서만")
        params = {k: v for k, v in (params or {}).items() if v is not None}
        last = None
        for attempt in range(self.retries):
            ts = str(int(time.time() * 1000) + self.time_offset)
            try:
                if method == "GET":
                    qs = urlencode(params)
                    headers = self._headers(ts, qs)
                    resp = self.s.get(f"{self.base}{path}" + (f"?{qs}" if qs else ""), headers=headers, timeout=self.timeout)
                else:
                    body = json.dumps(params, separators=(",", ":"))
                    headers = self._headers(ts, body)
                    headers["Content-Type"] = "application/json"
                    resp = self.s.post(f"{self.base}{path}", data=body, headers=headers, timeout=self.timeout)
                if resp.status_code >= 500 or resp.status_code == 403:
                    last = BybitNetError(f"{path} HTTP {resp.status_code}")
                    if resp.status_code == 403:   # IP 과다 → 오래 기다려야 함, 바로 포기
                        raise last
                    time.sleep(2 ** attempt)
                    continue
                data = resp.json()
            except (requests.RequestException, ValueError) as e:
                last = BybitNetError(f"{path} {type(e).__name__}")
                time.sleep(2 ** attempt)
                continue
            code = data.get("retCode")
            if code == 0:
                return data.get("result") or {}
            if code in RETRY_CODES:
                if code == 10002:
                    self.sync_time()
                last = BybitError(code, data.get("retMsg"), path)
                time.sleep(2 ** attempt)
                continue
            raise BybitError(code, data.get("retMsg"), path)
        raise last or BybitNetError(f"{path} 실패")

    def _headers(self, ts, payload):
        return {"X-BAPI-API-KEY": self._key, "X-BAPI-TIMESTAMP": ts, "X-BAPI-RECV-WINDOW": self.recv,
                "X-BAPI-SIGN": self._sign(ts, payload)}
