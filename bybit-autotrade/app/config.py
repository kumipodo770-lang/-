"""
설정 읽기. 한도·예산·모드는 config.yaml 에서만 바뀐다 (웹·텔레그램에서는 못 바꿈).
비밀값(키·토큰·비밀번호)은 환경 변수에서만 읽는다. 값은 절대 출력·기록하지 않는다.
프로젝트 폴더의 .env 가 있으면 시작할 때 환경 변수로 올린다 (사용자가 직접 만드는 파일).
live 장부는 (1) mode: live (2) BYBIT_LIVE_API_KEY/SECRET 환경 변수 (3) LIVE_CONFIRM.txt 첫 줄이 정확한 문구
세 가지가 모두 있어야만 켜진다. 코드·웹·텔레그램 어디에도 live 로 바꾸는 기능은 없다.
"""
import os
import copy
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIVE_CONFIRM_FILE = os.path.join(ROOT, "LIVE_CONFIRM.txt")
LIVE_CONFIRM_PHRASE = "LIVE TRADING CONFIRMED BY OWNER"
STRATEGIES = ("H3", "H4", "H5")
MODES = ("paper", "demo", "live")

DEFAULTS = {
    "screener": {"url": "http://127.0.0.1:8000", "poll_sec": 20, "hplan_days": 2, "result_days": 10},
    "market": {"public_url": "https://api.bybit.com"},
    "risk": {
        "per_trade_risk_pct": 0.25,
        "per_trade_risk_cap_pct": 2.0,
        "max_risk_after_rounding_pct": 0.5,
        "daily_loss_limit_pct": 6.0,
        "cumulative_stop_pct": 20.0,
        "max_positions_per_strategy": 60,
        "leverage_cap": 5,
        "liq_buffer_pct": 5.0,
        "min_mmr_pct": 1.0,
        "fee_taker_pct": 0.055,
        "fee_maker_pct": 0.02,
        "cumulative_reset_id": 0,
        "api_fail_retries": 3,
    },
    "strategy": {
        "stop_loss_pct": 15.0,
        "h4_take_profit_pct": 12.0,
        "lock_profit_pct": 0.5,
        "h5_trigger_pct": 5.0,
        "hold_hours": {"H3": 72, "H4": 168, "H5": 168},
    },
    "books": [{"name": "paper", "mode": "paper", "budgets": {"H3": 300, "H4": 300, "H5": 300},
               "regime_gate": {"k1_down": "record", "low_vol": "record"}}],
    "telegram": {"enabled": True},
    "web": {"host": "0.0.0.0", "port": 8100},
    "paper": {"slippage_pct": 0.05, "start_equity": 10000},
    # 출금 (사용자 결정 2026-10-15): 바이비트 화이트리스트에 넣은 업비트 XRP 주소 1개로만. app/withdraw.py
    "withdraw": {"enabled": False, "coin": "XRP", "chain": "XRP", "address": "", "tag": "", "max_usd": 100, "count_per_day": 5,
                 "buffer_pct": 2.0, "upbit_fee_pct": 0.05, "upbit_krw_withdraw_fee": 1000, "spot_fee_pct": 0.1, "usd_krw": 0},
}

SECRET_NAMES = ("BYBIT_DEMO_API_KEY", "BYBIT_DEMO_API_SECRET", "BYBIT_LIVE_API_KEY", "BYBIT_LIVE_API_SECRET",
                "SCREENER_PASSWORD", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "DASH_PASSWORD")


def load_dotenv(path=None):
    """프로젝트 .env → 환경 변수 (이미 있으면 덮지 않음). 값은 어디에도 출력하지 않는다."""
    path = path or os.path.join(ROOT, ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v


def secret(name: str) -> str | None:
    assert name in SECRET_NAMES
    v = os.environ.get(name)
    return v if v else None


def _merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


class ConfigError(Exception):
    pass


def live_allowed() -> tuple[bool, str]:
    """live 장부를 켤 수 있는지. (가능 여부, 이유)"""
    if not (secret("BYBIT_LIVE_API_KEY") and secret("BYBIT_LIVE_API_SECRET")):
        return False, "live 키 환경 변수(BYBIT_LIVE_API_KEY/SECRET) 없음"
    if not os.path.exists(LIVE_CONFIRM_FILE):
        return False, "LIVE_CONFIRM.txt 없음"
    with open(LIVE_CONFIRM_FILE, encoding="utf-8-sig") as f:
        first = f.readline().strip()
    if first != LIVE_CONFIRM_PHRASE:
        return False, "LIVE_CONFIRM.txt 첫 줄 문구가 다름"
    return True, "ok"


def load(path=None) -> dict:
    path = path or os.path.join(ROOT, "config.yaml")
    raw = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    cfg = _merge(DEFAULTS, raw)
    if "books" in raw:
        cfg["books"] = raw["books"]
    validate(cfg)
    return cfg


def validate(cfg: dict):
    r = cfg["risk"]
    if not (0 < r["per_trade_risk_pct"] <= r["per_trade_risk_cap_pct"] <= 2.0):
        raise ConfigError("포지션당 위험: 0 < 목표 ≤ 상한 ≤ 2% 이어야 함")
    if not (0 < r["daily_loss_limit_pct"] <= 6.0):
        raise ConfigError("일일 손실 한도는 0~6%")
    if not (0 < r["cumulative_stop_pct"] <= 20.0):
        raise ConfigError("누적 정지는 0~20%")
    if not (1 <= r["leverage_cap"] <= 5):
        raise ConfigError("레버리지 상한은 1~5배")
    if cfg["strategy"]["stop_loss_pct"] <= 0:
        raise ConfigError("손절 % 필요")
    names = set()
    for b in cfg["books"]:
        if b.get("mode") not in MODES:
            raise ConfigError(f"장부 {b.get('name')}: mode 는 paper/demo/live")
        if b["name"] in names:
            raise ConfigError("장부 이름 중복")
        names.add(b["name"])
        for s, amt in (b.get("budgets") or {}).items():
            if s not in STRATEGIES:
                raise ConfigError(f"알 수 없는 전략 {s}")
            if amt is not None and float(amt) < 0:
                raise ConfigError("예산은 0 이상")
        g = b.setdefault("regime_gate", {"k1_down": "record", "low_vol": "record"})
        for k in ("k1_down", "low_vol"):
            if g.get(k, "record") not in ("block", "record"):
                raise ConfigError("regime_gate 값은 block 또는 record")
            g.setdefault(k, "record")


def write_budgets(path: str, book: str, budgets: dict, backup_dir: str | None = None) -> None:
    """config.yaml 에서 그 장부의 budgets 한 줄만 바꿔 저장 (주석·다른 줄 유지). 바꾼 뒤 다시 읽어 검증, 실패하면 원래대로.
    웹 화면의 예산 수정(사용자 결정 2026-10-13)이 쓰는 유일한 설정 쓰기 경로. 예산 외 값은 절대 바꾸지 않는다."""
    import re
    import time
    import shutil
    import tempfile
    with open(path, encoding="utf-8-sig") as f:
        lines = f.read().split("\n")
    start = None
    for i, ln in enumerate(lines):
        m = re.match(r"^(\s*)-\s*name:\s*([^\s#]+)\s*(#.*)?$", ln)
        if m and m.group(2).strip("'\"") == book:
            start = i
            break
    if start is None:
        raise ConfigError(f"설정에서 장부 {book} 을 못 찾음")
    target = None
    for j in range(start + 1, len(lines)):
        if re.match(r"^\s*-\s*name:", lines[j]) or re.match(r"^\S", lines[j]):
            break
        if re.match(r"^\s*budgets:\s*\{.*\}", lines[j]):
            target = j
            break
    if target is None:
        raise ConfigError(f"장부 {book} 의 budgets 줄을 못 찾음")
    m = re.match(r"^(\s*)budgets:\s*\{.*?\}(\s*#.*)?$", lines[target])
    indent, comment = m.group(1), (m.group(2) or "")
    body = ", ".join(f"{s}: {int(v) if float(v).is_integer() else float(v)}" for s, v in budgets.items())
    lines[target] = f"{indent}budgets: {{{body}}}{comment}"
    text = "\n".join(lines)
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".yaml")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    try:
        new = load(tmp)
        got = next(b for b in new["books"] if b["name"] == book)["budgets"]
        if {k: float(v) for k, v in got.items()} != {k: float(v) for k, v in budgets.items()}:
            raise ConfigError("저장 검증 실패")
        if backup_dir:
            os.makedirs(backup_dir, exist_ok=True)
            shutil.copy2(path, os.path.join(backup_dir, f"config_{time.strftime('%Y%m%d_%H%M%S')}.yaml"))
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def public_view(cfg: dict) -> dict:
    """웹에 보여줄 설정 (비밀값 없음, 읽기 전용)."""
    return {"risk": cfg["risk"], "strategy": cfg["strategy"],
            "books": [{"name": b["name"], "mode": b["mode"], "budgets": b.get("budgets"), "regime_gate": b.get("regime_gate")}
                      for b in cfg["books"]]}
