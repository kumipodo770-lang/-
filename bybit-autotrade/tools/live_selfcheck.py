"""
실계좌 키 점검 — 읽기만 (주문·취소·레버리지 변경 없음). 사용자가 check_live.bat 으로 직접 실행.
확인: 키 인증, 마진 모드(격리여야 함), 포지션 모드(원웨이여야 함), USDT 잔고, 열린 포지션·주문 수, live 켜기 조건.
키 값은 출력하지 않는다.
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import config as C
from app.bybit_rest import BybitREST, BybitError, BybitNetError


def main():
    C.load_dotenv()
    k, s = C.secret("BYBIT_LIVE_API_KEY"), C.secret("BYBIT_LIVE_API_SECRET")
    if not (k and s):
        print("✗ .env 에 BYBIT_LIVE_API_KEY / BYBIT_LIVE_API_SECRET 이 없습니다."); return
    r = BybitREST("live", k, s, retries=2)
    r.sync_time()
    ok = True
    try:
        info = r.call("GET", "/v5/account/info")
        mm = info.get("marginMode")
        print(f"{'✓' if mm == 'ISOLATED_MARGIN' else '✗'} 마진 모드: {mm}" + ("" if mm == "ISOLATED_MARGIN" else "  ← 바이비트에서 격리(Isolated)로 바꿔 주세요"))
        ok &= mm == "ISOLATED_MARGIN"
        w = r.call("GET", "/v5/account/wallet-balance", {"accountType": "UNIFIED", "coin": "USDT"})
        eq = next((c.get("equity") for a in w.get("list", []) for c in a.get("coin", []) if c.get("coin") == "USDT"), None)
        print(f"✓ USDT 평가금액: {eq}")
        p = r.call("GET", "/v5/position/list", {"category": "linear", "symbol": "BTCUSDT"})
        idx = {x.get("positionIdx") for x in p.get("list", [])}
        oneway = idx <= {0}
        print(f"{'✓' if oneway else '✗'} 포지션 모드: {'원웨이' if oneway else '양방향(헤지)'}" + ("" if oneway else "  ← 원웨이로 바꿔 주세요"))
        ok &= oneway
        ps = r.call("GET", "/v5/position/list", {"category": "linear", "settleCoin": "USDT", "limit": 200})
        n = sum(1 for x in ps.get("list", []) if float(x.get("size") or 0) > 0)
        print(f"✓ 열린 USDT 무기한 포지션 {n}개" + ("  (봇은 이 코인들의 신호는 건너뜁니다)" if n else ""))
        o = r.call("GET", "/v5/order/realtime", {"category": "linear", "settleCoin": "USDT", "limit": 50})
        print(f"✓ 열린 주문 {len(o.get('list', []))}개")
    except (BybitError, BybitNetError) as e:
        ok = False
        msg = str(e)
        hint = ""
        if "10003" in msg or "10004" in msg or "10005" in msg:
            hint = "  ← 키·시크릿이 틀렸거나 권한(Read-Write, Contract Orders·Positions)이 부족합니다"
        if "10010" in msg:
            hint = "  ← IP 제한에 이 PC 주소가 없습니다 (VPN 이 켜져 있지 않은지도 확인)"
        print(f"✗ 바이비트 응답 오류: {msg}{hint}")
    a, why = C.live_allowed()
    print(f"{'✓' if a else '·'} live 켜기 조건: {why}")
    print("\n결과:", "실계좌 연결 정상" if ok else "고칠 것이 있습니다 (✗ 표시)")


if __name__ == "__main__":
    main()
    input("\n엔터를 누르면 닫힙니다")
