"""
demo / live 거래소 (바이비트 V5). 키는 생성자에만 들어오고 이 객체 밖으로 나가지 않는다.
진입: 지정가 GTC + 붙임 손절(·익절) tpslMode=Partial → 체결된 수량만큼 거래소 서버에 손절·익절이 생김 (봇이 꺼져도 유지).
청산: 감소 전용 시장가. 손절 이동: 조건 주문 triggerPrice 수정(amend), 안 되면 엔진이 새 손절을 걸고 옛것을 취소.
문서 근거: order/create (tpslMode Partial, reduceOnly 면 TP/SL 불가), order/amend, order/realtime, position/list,
execution/list(7일 창), account/info(marginMode), position/set-leverage.
"""
import logging
from .bybit_rest import BybitREST, BybitError, BybitNetError
from .exchange_base import Exchange, Order, Position, Execution, ExchangeError
from .timeutil import now_ms, DAY

log = logging.getLogger("bybit_ex")


def _f(x, d=0.0):
    try:
        return float(x) if x not in (None, "") else d
    except ValueError:
        return d


def _fmt(x, step):
    """거래소 자릿수에 맞춘 문자열 (step 배수로 반올림)"""
    from decimal import Decimal, ROUND_HALF_UP
    st = Decimal(str(step)).normalize()
    q = (Decimal(str(x)) / st).quantize(Decimal(1), rounding=ROUND_HALF_UP) * st
    dec = max(0, -st.as_tuple().exponent)
    return f"{q:.{dec}f}"


class BybitExchange(Exchange):
    def __init__(self, mode, api_key, api_secret, market, retries=3):
        self.mode = mode
        self.rest = BybitREST(mode, api_key, api_secret, retries=retries)
        self.rest.sync_time()
        self.m = market

    def _call(self, method, path, params=None):
        try:
            return self.rest.call(method, path, params)
        except (BybitError, BybitNetError) as e:
            raise ExchangeError(str(e)) from None

    # ---------- 정보 ----------
    def margin_mode(self):
        return self._call("GET", "/v5/account/info").get("marginMode", "")

    def equity(self):
        res = self._call("GET", "/v5/account/wallet-balance", {"accountType": "UNIFIED", "coin": "USDT"})
        for acc in res.get("list", []):
            for c in acc.get("coin", []):
                if c.get("coin") == "USDT":
                    return _f(c.get("equity"))
            return _f(acc.get("totalEquity"))
        return 0.0

    def set_leverage(self, symbol, lev):
        try:
            self._call("POST", "/v5/position/set-leverage", {"category": "linear", "symbol": symbol,
                                                             "buyLeverage": str(lev), "sellLeverage": str(lev)})
        except ExchangeError as e:
            if "110043" in str(e):      # 이미 같은 레버리지
                return
            raise

    def last_price(self, symbol):
        return self.m.ticker(symbol)["last"]

    # ---------- 주문 ----------
    def _px(self, symbol, p):
        return _fmt(p, self.m.instrument(symbol)["tick"])

    def _qty(self, symbol, q):
        return _fmt(q, self.m.instrument(symbol)["qty_step"])

    def place_entry(self, symbol, qty, price, link, sl, tp=None):
        p = {"category": "linear", "symbol": symbol, "side": "Buy", "orderType": "Limit", "qty": self._qty(symbol, qty),
             "price": self._px(symbol, price), "timeInForce": "GTC", "positionIdx": 0, "orderLinkId": link,
             "tpslMode": "Partial", "stopLoss": self._px(symbol, sl), "slTriggerBy": "LastPrice", "slOrderType": "Market"}
        if tp:
            p.update({"takeProfit": self._px(symbol, tp), "tpTriggerBy": "LastPrice", "tpOrderType": "Market"})
        return self._call("POST", "/v5/order/create", p)["orderId"]

    def place_stop(self, symbol, qty, trigger, link):
        p = {"category": "linear", "symbol": symbol, "side": "Sell", "orderType": "Market", "qty": self._qty(symbol, qty),
             "triggerPrice": self._px(symbol, trigger), "triggerDirection": 2, "triggerBy": "LastPrice",
             "reduceOnly": True, "closeOnTrigger": True, "positionIdx": 0, "orderLinkId": link}
        return self._call("POST", "/v5/order/create", p)["orderId"]

    def amend_trigger(self, symbol, order_id, trigger):
        self._call("POST", "/v5/order/amend", {"category": "linear", "symbol": symbol, "orderId": order_id,
                                               "triggerPrice": self._px(symbol, trigger)})

    def cancel(self, symbol, order_id=None, link=None):
        p = {"category": "linear", "symbol": symbol}
        if order_id:
            p["orderId"] = order_id
        else:
            p["orderLinkId"] = link
        self._call("POST", "/v5/order/cancel", p)

    def close_market(self, symbol, qty, link):
        p = {"category": "linear", "symbol": symbol, "side": "Sell", "orderType": "Market", "qty": self._qty(symbol, qty),
             "reduceOnly": True, "positionIdx": 0, "orderLinkId": link}
        return self._call("POST", "/v5/order/create", p)["orderId"]

    # ---------- 조회 ----------
    @staticmethod
    def _order(x) -> Order:
        return Order(order_id=x.get("orderId", ""), link=x.get("orderLinkId", ""), symbol=x.get("symbol", ""),
                     side=x.get("side", ""), otype=x.get("orderType", ""), qty=_f(x.get("qty")), price=_f(x.get("price")),
                     trigger=_f(x.get("triggerPrice")), reduce_only=bool(x.get("reduceOnly")), status=x.get("orderStatus", ""),
                     cum_qty=_f(x.get("cumExecQty")), avg_price=_f(x.get("avgPrice")), stop_type=x.get("stopOrderType", ""),
                     parent_link=x.get("parentOrderLinkId", "") or "", created=int(_f(x.get("createdTime"))),
                     updated=int(_f(x.get("updatedTime"))))

    def open_orders(self):
        out, cursor = [], None
        for _ in range(50):                       # 쪽 수 상한 (끝없는 반복 방지)
            res = self._call("GET", "/v5/order/realtime", {"category": "linear", "settleCoin": "USDT", "openOnly": 0,
                                                           "limit": 50, "cursor": cursor})
            out += [self._order(x) for x in res.get("list", [])]
            cursor = res.get("nextPageCursor")
            if not cursor:
                break
        return [o for o in out if o.status in ("New", "PartiallyFilled", "Untriggered")]

    def order_by_link(self, symbol, link):
        for path in ("/v5/order/realtime", "/v5/order/history"):
            res = self._call("GET", path, {"category": "linear", "symbol": symbol, "orderLinkId": link})
            lst = res.get("list", [])
            if lst:
                return self._order(lst[0])
        return None

    def positions(self):
        res = self._call("GET", "/v5/position/list", {"category": "linear", "settleCoin": "USDT", "limit": 200})
        out = {}
        for x in res.get("list", []):
            if _f(x.get("size")) > 0 and x.get("side") == "Buy":
                out[x["symbol"]] = Position(x["symbol"], _f(x["size"]), _f(x["avgPrice"]), _f(x.get("leverage")),
                                            _f(x.get("liqPrice")), _f(x.get("markPrice")), _f(x.get("unrealisedPnl")))
            elif _f(x.get("size")) > 0:
                out[x["symbol"]] = Position(x["symbol"], -_f(x["size"]), _f(x["avgPrice"]))  # 숏 = 봇 것 아님
        return out

    def executions(self, since_ms):
        # 버그 수정 (10/13): 반복 조건에 '지금 시각'을 매번 다시 읽어서 끝나지 않던 문제 → 시작할 때 한 번만 고정
        out = []
        now = now_ms()
        start = max(since_ms, now - 7 * DAY + 60_000)
        windows = 0
        while start < now and windows < 2:
            windows += 1
            end = min(start + 7 * DAY - 1000, now)
            cursor, pages = None, 0
            while pages < 50:
                pages += 1
                res = self._call("GET", "/v5/execution/list", {"category": "linear", "startTime": start, "endTime": end,
                                                               "limit": 100, "cursor": cursor})
                for x in res.get("list", []):
                    out.append(Execution(exec_id=x["execId"], order_id=x.get("orderId", ""), link=x.get("orderLinkId", ""),
                                         symbol=x["symbol"], side=x.get("side", ""), qty=_f(x.get("execQty")),
                                         price=_f(x.get("execPrice")), fee=_f(x.get("execFee")), exec_type=x.get("execType", ""),
                                         time=int(_f(x.get("execTime"))), is_maker=bool(x.get("isMaker")),
                                         stop_type=x.get("stopOrderType", ""), extra={"createType": x.get("createType", "")}))
                cursor = res.get("nextPageCursor")
                if not cursor:
                    break
            start = end + 1
        return sorted(out, key=lambda e: e.time)

    def tick(self, now_ms_=None):
        return None
