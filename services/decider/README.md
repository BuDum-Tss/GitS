# decider: общий сервис классификации

Сервис отвечает на набор вопросов о тексте за один проход, без генерации: модель
`fastino/GLiNER2.5-multi-Decide` (287M, многоязычная, Apache-2.0) на процессоре. Им пользуются ядро GitS (менеджер внимания,
`agent/decide.py`, ТЗ 03) и `rimworld-module` (оценка игровых событий). Отдельный процесс нужен потому, что ядро работает на
Python 3.14, где torch может не ставиться, а модель не должна мешать ядру и Gemma на видеокарте.

Адрес: `http://127.0.0.1:8768` (`GITS_DECIDER_PORT` меняет порт, `GITS_DECIDER_URL` у клиента).

## Установка

Python 3.12 (на 3.14 колёс torch может не быть):

```powershell
C:\Users\coffe\AppData\Local\Python\pythoncore-3.12-64\python.exe -m venv services\decider\.venv
services\decider\.venv\Scripts\python.exe -m pip install --extra-index-url https://download.pytorch.org/whl/cpu -r services\decider\requirements.lock.txt
```

- `requirements.txt`: прямые зависимости (`torch==2.14.1+cpu` с индекса PyTorch для процессора, `gliner2==2.0.0`,
  `transformers==5.18.0`, `aiohttp==3.14.3`).
- `requirements.lock.txt`: полный `pip freeze` окружения, на котором сервис проверен (без него транзитивные версии уходят вперёд:
  `huggingface_hub` 2.x тянет новый `httpx2`).
- Веса не скачиваются при запуске: `HF_HOME` указывает на `services/decider/hf` (там кэш `fastino/GLiNER2.5-multi-Decide`,
  около 1,1 ГБ), включён `HF_HUB_OFFLINE`. Каталоги `.venv/` и `hf/` в git не входят. Весов нет - скопируй каталог `hf` из
  другой копии или один раз скачай модель командой `hf download fastino/GLiNER2.5-multi-Decide` с `HF_HOME` на эту папку.

## Запуск

Обычно сервис запускает супервизор ядра (запись `decider` в `agent/modules.yaml`, `kind: service`): он следит за
`GET /health` и перезапускает процесс. Если порт уже занят работающим сервисом, второй экземпляр не запускается. Вручную:

```powershell
cd services\decider
.venv\Scripts\python.exe decider_server.py
```

Загрузка модели около 15 секунд, потом `GET /health` отвечает `{"ok": true}`.

## Протокол

```
POST /v1/systemone
  {"state": "<текст до 6000 символов>",
   "questions": {"<id>": {"type": "choice", "options": {"<метка>": "<описание метки>"}},
                 "<id>": {"type": "noul"}}}
-> {"answers": {"<id>": {"type": "choice", "answer": "<метка>", "confidence": 0.62, "probabilities": {"<метка>": 0.62}}},
    "ms": 280}
GET /health -> {"ok": true, "model": "fastino/GLiNER2.5-multi-Decide"}
```

- `choice`: `options` список или словарь `{метка: описание}`; модель видит описание (метки на английском с описаниями
  работают точнее русских; входной текст русский). Описание метки и есть вопрос: поле `instructions` сервер не использует.
- `noul`: да/нет, `answer` равен `yes` или `no`. Модель при этом видит только слова yes и no, без текста вопроса, поэтому
  ответ сильно смещён в «нет».
- Модель выдаёт выбранную метку и уверенность, вероятности остальных не сообщает.
- Вопросы выполняются по очереди (одна модель). Категории модель различает уверенно, абстрактные порядковые вопросы («важность
  от 0 до 5») почти наугад, поэтому ядро спрашивает только категории, а срочность выводит кодом.

## Проверка

```powershell
.venv\Scripts\python.exe eval_decider.py              # точность на размеченных русских примерах
.venv\Scripts\python.exe eval_decider.py --bench 200  # задержка p50 и p95 на 200 текстах до 400 символов
```
