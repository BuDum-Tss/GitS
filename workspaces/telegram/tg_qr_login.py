"""Вход в Telegram-аккаунт по QR-коду (Telethon): общий код скрипта tg_login.py и окружения (панель дашборда).

Шаг входа один и тот же: `ExportLoginTokenRequest` раз в `POLL_S` секунд (подтверждение на телефоне видно по ответу, а не по
обновлению, которое может не дойти); аккаунт в другом дата-центре (`LoginTokenMigrateTo`) принимает токен через
`ImportLoginTokenRequest`; пароль двух шагов приходит как `SessionPasswordNeededError`. Это `qr_poll`, его зовёт и терминальный
скрипт, и автомат `QrLogin`.

`QrLogin` это автомат для окружения: состояния idle, waiting (ждёт скана), password (нужен пароль двух шагов), done, error.
Он сам держит клиента Telethon на время входа, пишет картинку QR (PNG) в каталог, который окружение отдаёт дашборду через
`panel.serve_dirs`, и после успеха отпускает файл сессии, чтобы слушатель аккаунта мог её подхватить без перезапуска.
Пароль двух шагов приходит только в `submit_password`, живёт в одной локальной переменной до `sign_in` и нигде не пишется:
ни в журнал, ни в состояние, ни в текст ошибки.
"""
import asyncio
import base64
import hashlib
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("tg_qr_login")

POLL_S = 2.0                      # как часто спрашивать у Telegram, подтвердили ли вход на телефоне
WAIT_MAX_S = 300.0                # дольше ждать скана или пароля не будем: вход отменяется сам
QR_PREFIX, QR_SUFFIX = "qr-", ".png"

IDLE, WAITING, PASSWORD, DONE, ERROR = "idle", "waiting", "password", "done", "error"


class QrUnavailable(RuntimeError):
    """Нет пакета segno: картинку QR сделать нечем."""


@dataclass
class QrStep:
    """Итог одного опроса: token (показать QR и ждать), success, password (нужен пароль двух шагов), unknown."""
    kind: str
    token: bytes = b""
    expires: float = 0.0          # unix-время, когда код перестанет работать


def login_url(token: bytes) -> str:
    """Адрес из токена: его кодирует QR, телефон открывает его как «Подключить устройство»."""
    return "tg://login?token=" + base64.urlsafe_b64encode(token).decode().rstrip("=")


def qr_png(url: str) -> bytes:
    """PNG с QR-кодом адреса (segno). Тёмное на белом с полями: при тёмной теме экрана тоже читается телефоном."""
    try:
        import io

        import segno
    except ImportError:
        raise QrUnavailable("для QR нужен пакет segno (pip install segno)") from None
    buf = io.BytesIO()
    segno.make(url, error="m").save(buf, kind="png", scale=10, border=4, dark="black", light="white")
    return buf.getvalue()


async def qr_poll(client, api_id: int, api_hash: str) -> QrStep:
    """Один опрос входа по QR. Успех вызывает `client._on_login`, как это делает сам Telethon, и сессия сохраняется."""
    from telethon.errors import SessionPasswordNeededError
    from telethon.tl.functions.auth import ExportLoginTokenRequest, ImportLoginTokenRequest
    from telethon.tl.types.auth import LoginToken, LoginTokenMigrateTo, LoginTokenSuccess
    try:
        res = await client(ExportLoginTokenRequest(api_id, api_hash, []))
        if isinstance(res, LoginTokenMigrateTo):                      # аккаунт живёт в другом дата-центре Telegram
            await client._switch_dc(res.dc_id)
            res = await client(ImportLoginTokenRequest(res.token))
    except SessionPasswordNeededError:
        return QrStep("password")
    if isinstance(res, LoginTokenSuccess):
        client._on_login(res.authorization.user)
        return QrStep("success")
    if isinstance(res, LoginToken):
        return QrStep("token", res.token, res.expires.timestamp())
    return QrStep("unknown")


def is_network_error(e: BaseException) -> bool:
    """Нет сети (а не отказ Telegram): обрыв соединения, тайм-аут, недоступный адрес."""
    return isinstance(e, (OSError, ConnectionError, asyncio.TimeoutError, TimeoutError))


class QrLogin:
    """Автомат входа по QR для окружения. Один вход за раз; `start`, `cancel`, `submit_password`, `view`."""

    def __init__(self, session_path: str, api_id: str, api_hash: str, out_dir, *, client_factory=None, render=qr_png,
                 poll_s: float = POLL_S, wait_max_s: float = WAIT_MAX_S, clock=time.time, on_done=None, on_change=None):
        """session_path - файл сессии Telethon (без .session); out_dir - куда класть PNG; client_factory() - новый клиент
        Telethon (тесты подставляют свой); render(url) -> bytes PNG; on_done - async-функция после успешного входа (клиент уже
        закрыт, сессия на диске); on_change - обычная функция при каждой смене состояния (будит панель)."""
        self._session = session_path
        self._api_id, self._api_hash = api_id, api_hash
        self.out_dir = Path(out_dir)
        self._factory = client_factory or self._default_client
        self._render = render
        self._poll_s, self._wait_max_s, self._clock = poll_s, wait_max_s, clock
        self._on_done, self._on_change = on_done, on_change
        self._task: asyncio.Task | None = None
        self._passwords: asyncio.Queue = asyncio.Queue(maxsize=1)
        self.state, self.error, self.who = IDLE, "", ""
        self.expires, self.qr_file, self._token = 0.0, "", b""

    def _default_client(self):
        from telethon import TelegramClient
        return TelegramClient(self._session, int(self._api_id), self._api_hash)

    # ── управление ───────────────────────────────────────────────────────────

    @property
    def active(self) -> bool:
        """Идёт вход (держит файл сессии): слушатель аккаунта в это время к нему не обращается."""
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        """Начать вход. Уже идущий вход не перезапускается."""
        if self.active:
            return
        self._set(WAITING, error="", who="")
        self.expires, self._token = 0.0, b""
        self._passwords = asyncio.Queue(maxsize=1)
        self._task = asyncio.get_running_loop().create_task(self._run())

    async def cancel(self) -> None:
        """Отменить вход: клиент закрывается, картинка QR удаляется."""
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._clear_files()
        self._set(IDLE, error="", who="")

    async def wait(self) -> None:
        """Дождаться конца входа (для тестов)."""
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)

    def submit_password(self, password: str) -> bool:
        """Пароль двух шагов от пользователя. False, если пароль сейчас не нужен или уже передан."""
        if self.state != PASSWORD or not isinstance(password, str) or not password:
            return False
        try:
            self._passwords.put_nowait(password)
        except asyncio.QueueFull:
            return False
        return True

    def view(self) -> dict:
        """Состояние для панели, без секретов: state, error, who, qr_file и сколько секунд осталось у кода."""
        left = max(0, int(self.expires - self._clock())) if self.state == WAITING and self.expires else 0
        return {"state": self.state, "error": self.error, "who": self.who, "qr_file": self.qr_file, "expires_in": left}

    # ── ход входа ────────────────────────────────────────────────────────────

    def _set(self, state: str, **fields) -> None:
        self.state = state
        for key, value in fields.items():
            setattr(self, key, value)
        if self._on_change is not None:
            try:
                self._on_change()
            except Exception as e:                                   # будильник панели входу не помеха
                logger.debug("on_change: %s", e)

    def _qr_path(self, token: bytes) -> Path:
        return self.out_dir / f"{QR_PREFIX}{hashlib.sha1(token).hexdigest()[:8]}{QR_SUFFIX}"

    def _clear_files(self) -> None:
        for old in self.out_dir.glob(f"{QR_PREFIX}*{QR_SUFFIX}*"):
            old.unlink(missing_ok=True)
        self.qr_file, self._token = "", b""

    def _show(self, step: QrStep) -> None:
        """Новый код: PNG пишется во временный файл и подменяется (браузер не видит недописанного), старый удаляется."""
        self.expires = step.expires
        if step.token == self._token:
            return
        self.out_dir.mkdir(parents=True, exist_ok=True)
        path = self._qr_path(step.token)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(self._render(login_url(step.token)))
        os.replace(tmp, path)
        old = self.qr_file
        self._token, self.qr_file = step.token, path.name
        if old and old != path.name:
            (self.out_dir / old).unlink(missing_ok=True)
        self._set(WAITING)

    async def _run(self) -> None:
        client, ok = None, False
        try:
            client = self._factory()
            await client.connect()
            if await client.is_user_authorized():
                ok = True
            else:
                ok = await self._loop(client)
            if ok:
                me = await client.get_me()
                self.who = _who(me)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._fail(e)
        finally:
            await self._close(client)
        if ok:
            self._clear_files()
            self._set(DONE, error="")
            if self._on_done is not None:
                try:
                    await self._on_done()
                except Exception as e:
                    logger.warning("после входа: %s", type(e).__name__)

    async def _loop(self, client) -> bool:
        """Опрашивать Telegram, пока вход не подтвердят. True: вошли; False: ошибка (состояние уже выставлено)."""
        api_id = int(self._api_id)
        started = self._clock()
        while True:
            if self._clock() - started > self._wait_max_s:
                self._clear_files()
                self._set(ERROR, error="время ожидания вышло: код не отсканировали, начни вход заново")
                return False
            step = await qr_poll(client, api_id, self._api_hash)
            if step.kind == "success":
                return True
            if step.kind == "password":
                if not await self._password(client):
                    return False
                return True
            if step.kind == "token":
                self._show(step)
            await asyncio.sleep(self._poll_s)

    async def _password(self, client) -> bool:
        """Пароль двух шагов: ждём его из панели, неверный пароль даёт ещё попытку. Сам пароль нигде не сохраняется."""
        from telethon.errors import PasswordHashInvalidError
        self._clear_files()
        self._set(PASSWORD, error="")
        waited = self._clock()
        while True:
            try:
                password = await asyncio.wait_for(self._passwords.get(), timeout=max(1.0, self._wait_max_s - (self._clock() - waited)))
            except asyncio.TimeoutError:
                self._set(ERROR, error="время ожидания вышло: пароль не ввели, начни вход заново")
                return False
            try:
                await client.sign_in(password=password)
            except PasswordHashInvalidError:
                self._set(PASSWORD, error="неверный пароль двух шагов, попробуй ещё раз")
                continue
            finally:
                password = None
            return True

    def _fail(self, e: BaseException) -> None:
        """Ошибка входа в понятных словах; текст исключения не показывается целиком (в нём может быть что угодно)."""
        self._clear_files()
        name = type(e).__name__
        if isinstance(e, QrUnavailable):
            text = str(e)
        elif name == "FloodWaitError":
            text = f"Telegram просит подождать {getattr(e, 'seconds', '?')} с перед новой попыткой"
        elif is_network_error(e):
            text = "нет сети: не удалось связаться с Telegram"
        elif name == "PasswordHashInvalidError":
            text = "неверный пароль двух шагов"
        else:
            text = f"вход не удался ({name})"
        logger.warning("вход по QR: %s", text)
        self._set(ERROR, error=text)

    @staticmethod
    async def _close(client) -> None:
        if client is None:
            return
        try:
            await asyncio.shield(client.disconnect())
        except Exception as e:
            logger.debug("отключение клиента входа: %s", type(e).__name__)


def _who(me) -> str:
    """«Имя @username» из аккаунта Telethon."""
    name = getattr(me, "first_name", None) or ""
    user = getattr(me, "username", None)
    return " ".join(x for x in (name, f"@{user}" if user else "") if x) or "аккаунт"
