"""
거래소 공통 인터페이스. PaperExchange(가상 체결)와 BybitExchange(demo/live)가 같은 함수를 갖는다 → 엔진 코드는 하나.
모든 주문은 롱(Buy) 진입 + 감소 전용(Sell) 청산만 쓴다. 원웨이 모드(positionIdx 0).
"""
from dataclasses import dataclass, field, asdict


@dataclass
class Order:
    order_id: str
    link: str
    symbol: str
    side: str            # Buy / Sell
    otype: str           # Limit / Market
    qty: float
    price: float = 0.0
    trigger: float = 0.0
    reduce_only: bool = False
    status: str = "New"  # New PartiallyFilled Untriggered Filled Cancelled Rejected Triggered Deactivated ...
    cum_qty: float = 0.0
    avg_price: float = 0.0
    stop_type: str = ""  # "", PartialStopLoss, PartialTakeProfit, Stop ...
    parent_link: str = ""
    created: int = 0
    updated: int = 0

    def to_dict(self):
        return asdict(self)


@dataclass
class Position:
    symbol: str
    size: float
    avg_price: float
    leverage: float = 0.0
    liq_price: float = 0.0
    mark: float = 0.0
    upnl: float = 0.0


@dataclass
class Execution:
    exec_id: str
    order_id: str
    link: str
    symbol: str
    side: str
    qty: float
    price: float
    fee: float           # 양수 = 냄 (펀딩도 같은 부호: 양수 = 냄)
    exec_type: str       # Trade / Funding / BustTrade ...
    time: int
    is_maker: bool = False
    stop_type: str = ""
    extra: dict = field(default_factory=dict)


ACTIVE = {"New", "PartiallyFilled", "Untriggered", "Created", "Active"}
CLOSED = {"Filled", "Cancelled", "Rejected", "PartiallyFilledCanceled", "Deactivated", "Triggered"}


class ExchangeError(Exception):
    """거래소가 거부하거나 재시도 후에도 실패한 경우"""


class Exchange:
    mode = "base"

    def margin_mode(self) -> str: ...
    def equity(self) -> float: ...
    def set_leverage(self, symbol: str, lev: int): ...
    def place_entry(self, symbol, qty, price, link, sl, tp=None) -> str: ...
    def place_stop(self, symbol, qty, trigger, link) -> str: ...
    def amend_trigger(self, symbol, order_id, trigger): ...
    def cancel(self, symbol, order_id=None, link=None): ...
    def close_market(self, symbol, qty, link) -> str: ...
    def open_orders(self) -> list[Order]: ...
    def order_by_link(self, symbol, link) -> Order | None: ...
    def positions(self) -> dict[str, Position]: ...
    def executions(self, since_ms: int) -> list[Execution]: ...
    def last_price(self, symbol) -> float: ...
    def tick(self, now_ms: int): ...      # paper 만 의미 있음
