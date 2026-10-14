"""
텔레그램 알림 + 명령. 토큰·chat id 는 환경 변수(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID).
- 명령은 등록된 chat id 에서 온 것만 받는다. 다른 사람이 보내면 무시(로그만).
- /상태(/status) /정지(/stop) /재개(/resume) /청산(/closeall) — 정지·재개·청산은 버튼으로 한 번 더 확인 (60초 안).
- 한도 올리기·모드 바꾸기 명령은 없다.
- chat id 가 없으면 /start 를 보낸 사람의 chat id 를 웹 화면(로그인 필요)에만 보여 준다 → 사용자가 .env 에 넣음.
"""
import time
import queue
import secrets
import logging
import threading
import requests

log = logging.getLogger("telegram")


class Telegram:
    def __init__(self, token, chat_id, on_command=None):
        self._token = token
        self.chat_id = str(chat_id) if chat_id else None
        self.on_command = on_command          # fn(cmd, book) -> str
        self.q = queue.Queue()
        self.pending = {}                     # nonce → (cmd, book, expires)
        self.offset = 0
        self.seen_chat = None                 # chat id 미등록일 때 /start 보낸 chat
        self.ok = bool(token)
        self.last_error = None
        self._stop = False

    def _api(self, method, **params):
        try:
            r = requests.post(f"https://api.telegram.org/bot{self._token}/{method}", json=params, timeout=35)
            d = r.json()
        except Exception as e:      # 예외 문장에 주소(토큰 포함)가 들어갈 수 있어 종류만 남김
            raise RuntimeError(f"telegram {method}: 연결 오류 {type(e).__name__}") from None
        if not d.get("ok"):
            desc = str(d.get("description") or "").replace(self._token, "***")
            raise RuntimeError(f"telegram {method}: {desc}")
        return d["result"]

    def _note_error(self, e):
        msg = str(e).replace(self._token, "***")[:200]
        if msg != self.last_error:
            log.warning("텔레그램 오류: %s", msg)
        self.last_error = msg

    def send(self, text, buttons=None):
        if self.ok and self.chat_id:
            self.q.put((text, buttons))

    def start(self):
        if not self.ok:
            return
        threading.Thread(target=self._sender, daemon=True, name="tg-send").start()
        threading.Thread(target=self._poller, daemon=True, name="tg-poll").start()

    def _sender(self):
        while not self._stop:
            text, buttons = self.q.get()
            for a in range(3):
                try:
                    p = {"chat_id": self.chat_id, "text": text[:4000]}
                    if buttons:
                        p["reply_markup"] = {"inline_keyboard": [buttons]}
                    self._api("sendMessage", **p)
                    break
                except Exception as e:
                    self._note_error(e)
                    time.sleep(2 ** a)

    def _poller(self):
        while not self._stop:
            try:
                ups = self._api("getUpdates", offset=self.offset, timeout=25, allowed_updates=["message", "callback_query"])
                for u in ups:
                    self.offset = u["update_id"] + 1
                    self._handle(u)
                if self.last_error:
                    log.info("텔레그램 연결 회복")
                    self.last_error = None
            except Exception as e:
                self._note_error(e)
                time.sleep(5)

    def _handle(self, u):
        if "callback_query" in u:
            cq = u["callback_query"]
            chat = str(cq["message"]["chat"]["id"])
            try:
                self._api("answerCallbackQuery", callback_query_id=cq["id"])
            except Exception:
                pass
            if chat != self.chat_id:
                return
            nonce = cq.get("data", "")
            if nonce == "cancel":
                self.send("취소했습니다.")
                return
            item = self.pending.pop(nonce, None)
            if not item or item[2] < time.time():
                self.send("확인 시간이 지났습니다. 다시 보내 주세요.")
                return
            cmd, book = item[0], item[1]
            self.send(self.on_command(cmd, book) if self.on_command else "처리기 없음")
            return
        msg = u.get("message") or {}
        chat = str((msg.get("chat") or {}).get("id", ""))
        text = (msg.get("text") or "").strip()
        if not self.chat_id:
            if text.startswith("/start"):
                self.seen_chat = chat
                log.info("텔레그램 /start 받음 — 답장으로 chat id 를 보냄 (아직 미등록)")
                try:      # 아직 등록 전: 보낸 사람에게 자기 chat id 만 알려 줌 (명령은 등록 전엔 전부 무시)
                    self._api("sendMessage", chat_id=chat,
                              text=f"이 대화의 chat id: {chat}\n.env 의 TELEGRAM_CHAT_ID= 뒤에 이 숫자를 넣고 봇을 다시 시작하세요.")
                except Exception as e:
                    self._note_error(e)
            return
        if chat != self.chat_id:
            log.warning("등록 안 된 chat 의 메시지 무시")
            return
        parts = text.split()
        if not parts:
            return
        cmd = {"/status": "status", "/상태": "status", "/stop": "stop", "/정지": "stop", "/resume": "resume",
               "/재개": "resume", "/closeall": "closeall", "/청산": "closeall", "/start": "status", "/help": "help"}.get(parts[0])
        book = parts[1] if len(parts) > 1 else "all"
        if cmd is None or cmd == "help":
            self.send("명령: /상태  /정지 [장부]  /재개 [장부]  /청산 [장부]\n장부를 안 쓰면 전체. 정지·재개·청산은 버튼으로 한 번 더 확인합니다.")
            return
        if cmd == "status":
            self.send(self.on_command("status", book) if self.on_command else "-")
            return
        nonce = secrets.token_hex(8)
        self.pending[nonce] = (cmd, book, time.time() + 60)
        label = {"stop": "정지(신규 진입 중단·대기 주문 취소)", "resume": "재개", "closeall": "전체 청산(시장가)"}[cmd]
        self.send(f"[{book}] {label} 할까요? 60초 안에 눌러 주세요.",
                  buttons=[{"text": "확인", "callback_data": nonce}, {"text": "취소", "callback_data": "cancel"}])
