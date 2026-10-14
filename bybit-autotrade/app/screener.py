"""
스크리너 읽기 전용 클라이언트: /login (비밀번호 = 환경 변수 SCREENER_PASSWORD) → 쿠키 → /api/hplan.
스크리너에 쓰는 요청은 로그인 POST 하나뿐이고 서버 상태를 바꾸지 않는다.
"""
import logging
import requests

log = logging.getLogger("screener")


class Screener:
    def __init__(self, url, password, timeout=15):
        self.url = url.rstrip("/")
        self._pw = password
        self.s = requests.Session()
        self.timeout = timeout

    def _login(self):
        if not self._pw:
            raise RuntimeError("SCREENER_PASSWORD 환경 변수 없음")
        r = self.s.post(f"{self.url}/login", data={"password": self._pw}, allow_redirects=False, timeout=self.timeout)
        if r.status_code not in (302, 303) or "screener_auth" not in self.s.cookies:
            raise RuntimeError(f"스크리너 로그인 실패 HTTP {r.status_code}")

    def hplan(self, days=2) -> dict:
        for attempt in range(2):
            r = self.s.get(f"{self.url}/api/hplan", params={"days": days}, timeout=self.timeout)
            if r.status_code == 401:
                self._login()
                continue
            r.raise_for_status()
            d = r.json()
            if not d.get("ready"):
                raise RuntimeError("스크리너 hplan 준비 안 됨")
            return d
        raise RuntimeError("스크리너 인증 실패")
