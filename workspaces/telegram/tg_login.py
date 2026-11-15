"""Один раз войти в Telegram-аккаунт ГЛаДОС (Telethon). Запускается ВРУЧНУЮ в терминале:

    python workspaces/telegram/tg_login.py

Спросит номер телефона аккаунта, код из Telegram и, если включена, пароль двух шагов. Всё это вводишь ты сам:
ассистентка и инструменты этих данных не видят. После входа в папке памяти (MEMORY_DIR, по умолчанию agent/memory)
остаётся сессия tg_user.session.
Нужны TELEGRAM_API_ID и TELEGRAM_API_HASH в .env (my.telegram.org, API development tools).

Если код не доходит вообще, вход по QR (нужен пакет segno): python workspaces/telegram/tg_login.py --qr
Код приходит в приложение Telegram на устройстве, где этот аккаунт уже открыт (служебный чат «Telegram»), а если аккаунт нигде не
открыт, по SMS. Не пришёл: нажми Enter вместо кода, и Telegram пришлёт его следующим способом (SMS, звонок).
"""
import asyncio
import base64
import getpass
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
# SDK до установки пакета gits-sdk (как в main.py): без этого скрипт не находит gits_sdk, когда его запускают отдельно
sys.path[:0] = [str(HERE), str(HERE.parent.parent / "agent" / "interfaces" / "workspace")]

from common import default_memory_dir, secrets_file  # noqa: E402

try:
    from dotenv import load_dotenv
    load_dotenv(secrets_file())
except ImportError:
    pass

from telethon import TelegramClient  # noqa: E402
from telethon.errors import FloodWaitError, PhoneCodeExpiredError, PhoneCodeInvalidError, SessionPasswordNeededError  # noqa: E402

WAYS = {
    "SentCodeTypeApp": "в приложение Telegram на устройстве, где этот аккаунт уже открыт (служебный чат «Telegram»)",
    "SentCodeTypeSms": "по SMS на этот номер",
    "SentCodeTypeCall": "звонком: робот продиктует код",
    "SentCodeTypeFlashCall": "звонком-«мигалкой»: код это последние цифры номера звонящего",
    "SentCodeTypeMissedCall": "пропущенным звонком: код это последние цифры номера звонящего",
    "SentCodeTypeEmailCode": "на электронную почту аккаунта",
    "SentCodeTypeFragmentSms": "SMS через Fragment (анонимный номер)",
}


def _way(kind) -> str:
    return WAYS.get(type(kind).__name__, type(kind).__name__) if kind is not None else "неизвестно"


def _describe(sent) -> None:
    print(f"Код отправлен: {_way(getattr(sent, 'type', None))}.")
    nxt = getattr(sent, "next_type", None)
    if nxt is not None:
        print(f"Если не придёт, нажми Enter: следующий способ: {_way(nxt)}.")
    timeout = getattr(sent, "timeout", None)
    if timeout:
        print(f"(Повторный запрос возможен примерно через {timeout} с.)")


async def _login_qr(client) -> None:
    """Вход по QR-коду: код доставки не нужен. В Telegram на телефоне: Настройки -> Устройства -> Подключить устройство,
    навести камеру на код. По умолчанию код пишется картинкой PNG (в терминале QR часто не сканируется из-за тёмной темы и
    шрифта), файл обновляется каждые 30 секунд и удаляется при выходе; `--ascii` рисует его ещё и в терминале.
    Путь картинки: TG_QR_PNG, иначе /data/tg_login_qr.png (папка персонажа на хосте), иначе рядом со скриптом."""
    try:
        import segno
    except ImportError:
        print("Для QR нужен пакет segno: pip install segno")
        raise SystemExit(1) from None
    from telethon.tl.functions.auth import ExportLoginTokenRequest, ImportLoginTokenRequest
    from telethon.tl.types.auth import LoginToken, LoginTokenMigrateTo, LoginTokenSuccess

    png = Path(os.getenv("TG_QR_PNG") or (Path("/data") if Path("/data").is_dir() else HERE) / "tg_login_qr.png")
    ascii_too = "--ascii" in sys.argv[1:]
    api_id, api_hash = int(os.getenv("TELEGRAM_API_ID", "0")), os.getenv("TELEGRAM_API_HASH", "")
    shown, started, last_note = None, time.monotonic(), 0.0
    try:
        while True:
            # Опрос вместо ожидания события: подтверждение на телефоне видно по ответу, а не по обновлению, которое может не дойти
            try:
                res = await client(ExportLoginTokenRequest(api_id, api_hash, []))
                if isinstance(res, LoginTokenMigrateTo):                    # аккаунт живёт в другом дата-центре Telegram
                    await client._switch_dc(res.dc_id)
                    res = await client(ImportLoginTokenRequest(res.token))
            except SessionPasswordNeededError:
                await client.sign_in(password=await asyncio.to_thread(getpass.getpass, "Пароль двух шагов (ввод не виден): "))
                return
            if isinstance(res, LoginTokenSuccess):
                client._on_login(res.authorization.user)
                return
            if not isinstance(res, LoginToken):
                print(f"Неожиданный ответ Telegram: {type(res).__name__}")
                await asyncio.sleep(3)
                continue
            if res.token != shown:                                        # новый код: картинку перезаписываем
                shown = res.token
                url = "tg://login?token=" + base64.urlsafe_b64encode(res.token).decode().rstrip("=")
                segno.make(url, error="m").save(str(png), scale=12, border=4, dark="black", light="white")
                print(f"\nQR-код сохранён в файл: {png}")
                print("Если контейнер Docker: на хосте это resources\\GlaDOS\\tg_login_qr.png в папке проекта. Открой картинку.")
                print("Telegram на телефоне: Настройки -> Устройства -> Подключить устройство, наведи камеру на картинку.")
                print("После сканирования подтверди вход на телефоне (кнопка «Подтвердить»/«Войти»); скрипт сам это увидит.")
                if ascii_too:
                    segno.make(url).terminal(compact=True)
            if time.monotonic() - last_note >= 10:
                last_note = time.monotonic()
                print(f"... ждём подтверждения на телефоне ({int(time.monotonic() - started)} с); код истекает через "
                      f"{max(0, int(res.expires.timestamp() - time.time()))} с")
            await asyncio.sleep(2)
    finally:
        png.unlink(missing_ok=True)


async def main():
    memory = default_memory_dir()
    api_id, api_hash = os.getenv("TELEGRAM_API_ID", "").strip(), os.getenv("TELEGRAM_API_HASH", "").strip()
    if not (api_id and api_hash):
        print("В .env нет TELEGRAM_API_ID и TELEGRAM_API_HASH (получить: my.telegram.org -> API development tools).")
        return
    Path(memory).mkdir(parents=True, exist_ok=True)
    client = TelegramClient(str(Path(memory) / "tg_user"), int(api_id), api_hash)
    await client.connect()
    try:
        if await client.is_user_authorized():
            me = await client.get_me()
            print(f"Уже выполнен вход как {me.first_name} (@{me.username}).")
            return
        if "--qr" in sys.argv[1:]:
            await _login_qr(client)
            me = await client.get_me()
            print(f"Готово: вход выполнен как {me.first_name} (@{me.username}). Сессия сохранена в {memory}.")
            return
        phone = (await asyncio.to_thread(input, "Номер телефона аккаунта (с +, например +79991234567): ")).strip()
        try:
            sent = await client.send_code_request(phone)
        except FloodWaitError as e:
            print(f"Telegram просит подождать {e.seconds} с перед новой попыткой.")
            return
        _describe(sent)
        while True:
            code = (await asyncio.to_thread(input, "Код (Enter: прислать ещё раз другим способом, q: выйти): ")).strip().replace(" ", "")
            if code.lower() == "q":
                return
            if not code:
                try:
                    sent = await client.send_code_request(phone)      # повторный вызов у Telethon = auth.resendCode
                except FloodWaitError as e:
                    print(f"Подожди {e.seconds} с и нажми Enter снова.")
                    continue
                _describe(sent)
                continue
            try:
                await client.sign_in(phone, code)
                break
            except SessionPasswordNeededError:
                await client.sign_in(password=await asyncio.to_thread(getpass.getpass, "Пароль двух шагов (ввод не виден): "))
                break
            except PhoneCodeInvalidError:
                print("Код неверный, попробуй ещё раз.")
            except PhoneCodeExpiredError:
                print("Код устарел: запусти скрипт заново.")
                return
        me = await client.get_me()
        print(f"Готово: вход выполнен как {me.first_name} (@{me.username}). Сессия сохранена в {memory}.")
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
