"""텔레그램: 등록 전엔 /start 에 chat id 만 답장, 명령은 무시 / 등록 후엔 등록된 chat 의 명령만, 정지·청산은 확인 버튼"""
from app.notify import Telegram


class TG(Telegram):
    def __init__(self, chat_id, on_command=None):
        super().__init__("TOKEN", chat_id, on_command)
        self.sent = []

    def _api(self, method, **p):
        self.sent.append((method, p))
        return True


def msg(chat, text, uid=1):
    return {"update_id": uid, "message": {"chat": {"id": chat}, "text": text}}


def test_unregistered_start_replies_chat_id_and_ignores_commands():
    calls = []
    t = TG(None, lambda c, b: calls.append((c, b)) or "ok")
    t._handle(msg(555, "/start"))
    assert t.seen_chat == "555"
    assert t.sent and t.sent[0][0] == "sendMessage" and t.sent[0][1]["chat_id"] == "555" and "555" in t.sent[0][1]["text"]
    t._handle(msg(555, "/청산"))
    assert calls == []


def test_token_never_in_error_text(monkeypatch):
    import requests
    from app import notify as N
    t = N.Telegram("123:SECRETTOKEN", "1")

    def boom(url, **k):
        raise requests.ConnectionError(f"failed to reach {url}")
    monkeypatch.setattr(N.requests, "post", boom)
    try:
        t._api("getUpdates")
    except RuntimeError as e:
        assert "SECRETTOKEN" not in str(e)
    t._note_error(RuntimeError("x 123:SECRETTOKEN y"))
    assert "SECRETTOKEN" not in t.last_error


def test_registered_only_owner_and_confirm_needed():
    calls = []
    t = TG("100", lambda c, b: calls.append((c, b)) or "접수")
    t._handle(msg(999, "/청산"))                       # 다른 사람
    assert t.q.empty() and calls == []
    t._handle(msg(100, "/청산 live"))
    text, buttons = t.q.get_nowait()
    assert "확인" in buttons[0]["text"] and calls == []    # 아직 실행 안 됨
    nonce = buttons[0]["callback_data"]
    t._handle({"update_id": 2, "callback_query": {"id": "x", "data": nonce, "message": {"chat": {"id": 999}}}})
    assert calls == []                                    # 다른 chat 의 버튼은 무시
    t._handle({"update_id": 3, "callback_query": {"id": "y", "data": nonce, "message": {"chat": {"id": 100}}}})
    assert calls == [("closeall", "live")]
