"""Вход в аккаунт по QR из дашборда: автомат QrLogin (токен, миграция дата-центра, пароль двух шагов, отмена, тайм-аут, сбои),
выход из аккаунта и слова состояния «нет сессии» / «нет сети» / «ошибка связи». Сети Telegram нет: клиент Telethon подменён."""
import asyncio
import datetime
import logging
from types import SimpleNamespace

import pytest
import tg_qr_login
from telethon.errors import PasswordHashInvalidError, SessionPasswordNeededError
from telethon.tl.types.auth import Authorization, LoginToken, LoginTokenMigrateTo, LoginTokenSuccess
from tg_qr_login import DONE, ERROR, IDLE, PASSWORD, WAITING, QrLogin, qr_poll
from tg_rig import Rig

SECRET = "Пароль-Двух-Шагов-qwerty"


def token(n: int, ttl: float = 30.0) -> LoginToken:
    return LoginToken(expires=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=ttl),
                      token=f"tok{n}".encode())


def success() -> LoginTokenSuccess:
    return LoginTokenSuccess(Authorization(user=SimpleNamespace(first_name="Глэдис", username="glados")))


class FakeLoginClient:
    """Клиент Telethon для входа: отвечает по сценарию, запоминает вызовы."""

    def __init__(self, script, authorized=False, connect_error=None):
        self.script, self.authorized, self.connect_error = list(script), authorized, connect_error
        self.calls, self.connected, self.logged_in, self.dc, self.sign_ins = [], False, None, None, []

    async def connect(self):
        if self.connect_error:
            raise self.connect_error
        self.connected = True

    async def disconnect(self):
        self.connected = False

    async def is_user_authorized(self):
        return self.authorized

    async def __call__(self, request):
        self.calls.append(type(request).__name__)
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(item, Exception):
            raise item
        return item

    def _on_login(self, user):
        self.logged_in = user

    async def _switch_dc(self, dc_id):
        self.dc = dc_id

    async def sign_in(self, password=None):
        self.sign_ins.append(password)
        if password != SECRET:
            raise PasswordHashInvalidError(None)
        self.logged_in = SimpleNamespace(first_name="Глэдис", username="glados")

    async def get_me(self):
        return SimpleNamespace(first_name="Глэдис", username="glados")


def make(tmp_path, client, **kw):
    done = []

    async def on_done():
        done.append(client.connected)                                    # клиент к этому моменту уже закрыт

    login = QrLogin(str(tmp_path / "tg_user"), "1", "hash", tmp_path / "tg_login", client_factory=lambda: client,
                    render=lambda url: b"PNG:" + url.encode(), poll_s=0.01, on_done=on_done, **kw)
    return login, done


async def wait_state(login, state, timeout=3.0):
    for _ in range(int(timeout / 0.01)):
        if login.state == state:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"состояние {login.state!r}, ждали {state!r}")


# ── один опрос ──

async def test_poll_kinds_and_dc_migration():
    client = FakeLoginClient([LoginTokenMigrateTo(dc_id=4, token=b"mig"), success()])
    step = await qr_poll(client, 1, "h")
    assert step.kind == "success" and client.dc == 4 and client.logged_in.username == "glados"
    assert client.calls == ["ExportLoginTokenRequest", "ImportLoginTokenRequest"]
    step = await qr_poll(FakeLoginClient([token(1)]), 1, "h")
    assert step.kind == "token" and step.token == b"tok1" and step.expires > 0
    assert (await qr_poll(FakeLoginClient([SessionPasswordNeededError(None)]), 1, "h")).kind == "password"
    assert tg_qr_login.login_url(b"") == "tg://login?token=AQI"          # base64url без «=» в конце


# ── автомат ──

async def test_qr_login_shows_changing_code_then_succeeds_and_cleans_up(tmp_path):
    client = FakeLoginClient([token(1), token(1), token(2), success()])
    seen = []
    login, done = make(tmp_path, client, on_change=lambda: seen.append(login.state))
    await login.start()
    await wait_state(login, WAITING)
    for _ in range(300):
        if login.qr_file:
            break
        await asyncio.sleep(0.01)
    first = login.qr_file
    view = login.view()
    assert first.startswith("qr-") and first.endswith(".png") and view["state"] == WAITING and 0 < view["expires_in"] <= 30
    assert (tmp_path / "tg_login" / first).read_bytes().startswith(b"PNG:tg://login?token=")
    await login.wait()
    assert login.state == DONE and login.who == "Глэдис @glados" and login.view()["qr_file"] == ""
    assert list((tmp_path / "tg_login").glob("*")) == []                 # картинка удалена
    assert done == [False] and client.connected is False                 # on_done вызван после закрытия клиента
    assert WAITING in seen and DONE in seen


async def test_new_token_replaces_the_image_file(tmp_path):
    client = FakeLoginClient([token(1), token(1), token(2)])
    login, _ = make(tmp_path, client)
    await login.start()
    names = set()
    for _ in range(300):
        names.add(login.qr_file)
        if len(names - {""}) == 2:
            break
        await asyncio.sleep(0.01)
    files = [p.name for p in (tmp_path / "tg_login").glob("*") if not p.name.endswith(".tmp")]
    assert len(names - {""}) == 2 and files == [login.qr_file]            # одна картинка, старая удалена
    await login.cancel()


async def test_two_step_password_wrong_then_right_and_never_logged(tmp_path, caplog):
    client = FakeLoginClient([token(1), SessionPasswordNeededError(None)])
    login, done = make(tmp_path, client)
    with caplog.at_level(logging.DEBUG):
        await login.start()
        await wait_state(login, PASSWORD)
        assert login.view()["qr_file"] == "" and not login.submit_password("")           # пустой не принимается
        assert login.submit_password("неверный-пароль")
        for _ in range(300):
            if login.error:
                break
            await asyncio.sleep(0.01)
        assert login.state == PASSWORD and "неверный пароль" in login.error              # ещё попытка
        assert login.submit_password(SECRET)
        await login.wait()
    assert login.state == DONE and client.sign_ins == ["неверный-пароль", SECRET] and done
    dump = repr(login.view()) + repr(vars(login).get("error")) + caplog.text
    assert SECRET not in dump and "неверный-пароль" not in dump                          # пароль нигде не остаётся
    assert SECRET not in repr({k: v for k, v in vars(login).items() if k not in ("_factory", "_render", "_on_done", "_on_change")})


async def test_password_not_accepted_when_not_needed(tmp_path):
    login, _ = make(tmp_path, FakeLoginClient([token(1)]))
    assert login.submit_password(SECRET) is False                                         # вход не начат
    await login.start()
    await wait_state(login, WAITING)
    assert login.submit_password(SECRET) is False                                         # пароль не запрошен
    await login.cancel()


async def test_cancel_closes_client_and_removes_image(tmp_path):
    client = FakeLoginClient([token(1)])
    login, done = make(tmp_path, client)
    await login.start()
    for _ in range(300):
        if login.qr_file:
            break
        await asyncio.sleep(0.01)
    assert (tmp_path / "tg_login" / login.qr_file).exists()
    await login.cancel()
    assert login.state == IDLE and not login.active and client.connected is False
    assert list((tmp_path / "tg_login").glob("*")) == [] and done == []                   # on_done не зовётся
    await login.start()                                                                    # можно начать заново
    assert login.active
    await login.cancel()


async def test_start_twice_does_not_restart(tmp_path):
    login, _ = make(tmp_path, FakeLoginClient([token(1)]))
    await login.start()
    task = login._task
    await login.start()
    assert login._task is task
    await login.cancel()


async def test_timeout_gives_error_and_removes_image(tmp_path):
    login, done = make(tmp_path, FakeLoginClient([token(1)]), wait_max_s=0.15)
    await login.start()
    await wait_state(login, ERROR)
    assert "время ожидания" in login.error and done == []
    assert list((tmp_path / "tg_login").glob("*")) == []


async def test_network_failure_is_named_as_no_network(tmp_path):
    login, _ = make(tmp_path, FakeLoginClient([token(1)], connect_error=ConnectionError("boom: secret-detail")))
    await login.start()
    await wait_state(login, ERROR)
    assert login.error.startswith("нет сети") and "secret-detail" not in login.error


async def test_flood_wait_and_unknown_errors_have_plain_words(tmp_path):
    class FloodWaitError(Exception):
        seconds = 42

    login, _ = make(tmp_path, FakeLoginClient([FloodWaitError("x")]))
    await login.start()
    await wait_state(login, ERROR)
    assert "42 с" in login.error
    login2, _ = make(tmp_path / "b", FakeLoginClient([KeyError("внутренняя деталь")]))
    await login2.start()
    await wait_state(login2, ERROR)
    assert "KeyError" in login2.error and "внутренняя деталь" not in login2.error


async def test_missing_segno_is_explained(tmp_path):
    def no_segno(url):
        raise tg_qr_login.QrUnavailable("для QR нужен пакет segno (pip install segno)")

    client = FakeLoginClient([token(1)])
    login = QrLogin(str(tmp_path / "s"), "1", "h", tmp_path / "q", client_factory=lambda: client, render=no_segno, poll_s=0.01)
    await login.start()
    await wait_state(login, ERROR)
    assert "segno" in login.error


async def test_already_authorized_session_is_done_without_polling(tmp_path):
    client = FakeLoginClient([token(1)], authorized=True)
    login, done = make(tmp_path, client)
    await login.start()
    await login.wait()
    assert login.state == DONE and client.calls == [] and done


# ── слушатель аккаунта: вход, выход, слова состояния ──

async def test_user_login_flow_hands_session_to_the_listener(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, telethon=True) as rig:
        user = rig.mod.user
        user._unauthorized = True                                                          # сессия была недействительна
        assert user.state_word() == "нет сессии"
        client = FakeLoginClient([token(1), success()])
        user._login = QrLogin(str(rig.mem / "tg_user"), "1", "hash", user.qr_dir, client_factory=lambda: client,
                              render=lambda url: b"PNG", poll_s=0.01, on_done=user._login_done, on_change=user._changed)
        changes = []
        user.on_change = lambda: changes.append(user.state_word())
        await user.start_login()
        assert user.state_word() == "идёт вход" and user.login_active() and not user.state()[0]
        await user._login.wait()
        assert user._unauthorized is False and user._wake.is_set()                         # слушатель разбужен
        assert user.state_word() in ("подключается", "подключён") and changes


async def test_start_login_refuses_when_already_logged_in_or_not_configured(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, telethon=True) as rig:
        user = rig.mod.user
        user._me = SimpleNamespace(first_name="Глэдис", username="glados")
        with pytest.raises(RuntimeError, match="уже выполнен"):
            await user.start_login()
    monkeypatch.delenv("TELEGRAM_API_ID")
    monkeypatch.delenv("TELEGRAM_API_HASH")
    async with Rig(tmp_path / "b", monkeypatch) as rig:
        with pytest.raises(RuntimeError, match="TELEGRAM_API_ID"):
            await rig.mod.user.start_login()


class FakeTelethonClass:
    """Подмена telethon.TelegramClient для слушателя: поведение выбирает атрибут класса."""
    connect_error = None
    authorized = True
    instances = []

    def __init__(self, *a, **kw):
        FakeTelethonClass.instances.append(self)

    async def connect(self):
        if FakeTelethonClass.connect_error:
            raise FakeTelethonClass.connect_error

    async def disconnect(self):
        pass

    def is_connected(self):
        return True

    async def is_user_authorized(self):
        return FakeTelethonClass.authorized

    async def get_me(self):
        return SimpleNamespace(first_name="Глэдис", username="glados")

    def on(self, event):
        return lambda fn: fn

    async def run_until_disconnected(self):
        await asyncio.sleep(3600)


async def run_listener(rig, monkeypatch, **cls_attrs):
    import telethon
    for k, v in {"connect_error": None, "authorized": True, **cls_attrs}.items():
        monkeypatch.setattr(FakeTelethonClass, k, v)
    monkeypatch.setattr(telethon, "TelegramClient", FakeTelethonClass)
    del rig.mod.user._connect                                                              # настоящий _connect поверх подмены клиента
    task = asyncio.create_task(rig.mod.user.run())
    return task


async def test_state_word_distinguishes_no_session_no_network_and_error(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, telethon=True) as rig:
        user = rig.mod.user
        task = await run_listener(rig, monkeypatch, authorized=False)                      # файл сессии есть, но она недействительна
        try:
            await rig.core.wait_for(lambda: user._unauthorized)
            assert user.state_word() == "нет сессии" and "Войти по QR" in user.state()[1]
        finally:
            task.cancel()
    async with Rig(tmp_path / "net", monkeypatch, telethon=True) as rig:
        user = rig.mod.user
        task = await run_listener(rig, monkeypatch, connect_error=ConnectionError("нет маршрута"))
        try:
            await rig.core.wait_for(lambda: user._offline)
            assert user.state_word() == "нет сети" and user._problem == ""
        finally:
            task.cancel()
    async with Rig(tmp_path / "err", monkeypatch, telethon=True) as rig:
        user = rig.mod.user
        task = await run_listener(rig, monkeypatch, connect_error=KeyError("странный сбой"))
        try:
            await rig.core.wait_for(lambda: user._problem)
            assert user.state_word() == "ошибка связи" and not user._offline
        finally:
            task.cancel()
    async with Rig(tmp_path / "ok", monkeypatch, telethon=True) as rig:
        user = rig.mod.user
        task = await run_listener(rig, monkeypatch)
        try:
            await rig.core.wait_for(lambda: user._me is not None)
            assert user.state_word() == "подключён" and user.me_text() == "Глэдис @glados"
        finally:
            task.cancel()


async def test_logout_deletes_session_after_telegram_logout(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, telethon=True) as rig:
        user = rig.mod.user
        user._me = SimpleNamespace(first_name="Глэдис", username="glados")
        log = []

        async def log_out():
            log.append("log_out")
            return True

        rig.tl.log_out, rig.tl.disconnect = log_out, lambda: asyncio.sleep(0)
        user._client = rig.tl
        session = rig.mem / "tg_user.session"
        assert session.exists()
        text = await user.logout()
        assert "вышла" in text and log == ["log_out"] and not session.exists()
        assert user._me is None and user.state_word() == "нет сессии" and user._wake.is_set()
        assert "вход и так не выполнен" in await user.logout()                             # повторный выход ничего не ломает


async def test_logout_without_network_keeps_the_session(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, telethon=True) as rig:
        user = rig.mod.user

        async def log_out():
            raise ConnectionError("нет сети")

        rig.tl.log_out = log_out
        with pytest.raises(RuntimeError, match="нет сети"):
            await user.logout()
        assert (rig.mem / "tg_user.session").exists()                                      # иначе осталось бы «забытое» устройство


async def test_logout_refused_during_login(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, telethon=True) as rig:
        user = rig.mod.user
        user._login = QrLogin(str(rig.mem / "tg_user"), "1", "h", user.qr_dir, client_factory=lambda: FakeLoginClient([token(1)]),
                              render=lambda u: b"x", poll_s=0.01)
        await user.start_login()
        with pytest.raises(RuntimeError, match="идёт вход"):
            await user.logout()
        await user.cancel_login()
