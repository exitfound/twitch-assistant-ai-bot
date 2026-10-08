# TTS – сервер синтеза речи для голоса в Discord

Голос бота в Discord синтезирует отдельный TTS-сервер: модель **Qwen3-TTS 1.7B Base** с клонированием голоса, обёрнутая в OpenAI-совместимый HTTP API. Сервер работает на машине с видеокартой NVIDIA, бот обращается к нему по сети. Как бот использует голос – BOT.md, «Голос в Discord»; переменные бота – README.

## Схема

```
Twitch-чат ──► бот (контейнер в ВМ) ──HTTP──► TTS-сервер (Windows, GPU) ──PCM──► бот ──Opus──► голосовой канал Discord
```

- Бот отправляет готовый текст ответа на `POST /v1/audio/speech` и получает речь потоком: 16-битный моно PCM 24 кГц, кусками по мере генерации.
- Бот переводит звук в 48 кГц стерео, кодирует в Opus (libopus) и играет в голосовом канале.
- Сервер запускается вручную на время стрима. Без сервера бот отвечает в чате текстом и молчит в голосовом канале.

## Требования

| Что | Значение |
|---|---|
| ОС | Windows 10/11 |
| Видеокарта | NVIDIA, от 8 ГБ видеопамяти; сервер занимает 5–7 ГБ |
| Драйвер | с поддержкой CUDA 12.8; для RTX 50xx – 570 и новее |
| Python | 3.12 (сервер поддерживает 3.10–3.12) |
| Диск | ~24 ГБ: модели, окружение, кеши |
| Сеть | машина с ботом достаёт до порта сервера (8880) |

Процессор важен наравне с видеокартой: генерация упирается в один поток Python, который подаёт видеокарте мелкие шаги.

## Раскладка каталога

Всё лежит в одном каталоге, в систему ничего не прописывается. Удаление – удалить каталог.

```
D:\tts\qwen3\
├── server\                  исходники Qwen3-TTS-Openai-Fastapi (пакет qwen-tts в режиме -e)
├── venv\                    окружение Python 3.12
├── hf\                      кеш Hugging Face: скачанные модели
├── models\1.7B-t07\         рабочая модель: файлы 1.7B Base + свой generation_config.json
├── voice_library\profiles\  голосовые профили: <имя>\meta.json + reference.wav
├── config-1.7B-t07.yaml     конфиг сервера
├── start-tts.bat / .ps1     запуск: окно = сервер
├── stop-tts.bat             остановка
├── cache\                   кеши triton, inductor и временные файлы
└── logs\server-prod.log     лог сервера (перезаписывается при запуске)
```

## Установка

### 1. Окружение и сервер

```powershell
mkdir D:\tts\qwen3; cd D:\tts\qwen3
python -m venv venv
.\venv\Scripts\python.exe -m pip install --upgrade pip
# Исходники: github.com/groxaxo/Qwen3-TTS-Openai-Fastapi – архив или git clone в D:\tts\qwen3\server
.\venv\Scripts\pip.exe install torch==2.8.0 torchaudio==2.8.0 --index-url https://download.pytorch.org/whl/cu128
.\venv\Scripts\pip.exe install -e ".\server[api]"
.\venv\Scripts\pip.exe install triton-windows
```

- PyTorch – только сборка под CUDA 12.8 (`cu128`): более ранние не поддерживают архитектуру RTX 50xx.
- `triton-windows` обязателен: без него `torch.compile` не работает, и генерация медленнее речи (RTF ~1,6 против ~0,8–1,0).
- Проверка видеокарты: `.\venv\Scripts\python.exe -c "import torch; print(torch.cuda.get_device_name(0))"`.

Рабочие версии: Python 3.12.10, torch 2.8.0+cu128, triton-windows 3.4.0, transformers 4.57.3, fastapi 0.142.2, librosa 1.0.0.

### 2. Модель

```powershell
$env:HF_HOME = 'D:\tts\qwen3\hf'
.\venv\Scripts\python.exe -c "from huggingface_hub import snapshot_download; print(snapshot_download('Qwen/Qwen3-TTS-12Hz-1.7B-Base'))"
```

Рабочая копия модели – `models\1.7B-t07`: файлы снапшота (жёсткие ссылки, места не занимают) и свой `generation_config.json`. Параметры выборки сервер берёт **только** из этого файла:

```json
{
    "do_sample": true,
    "repetition_penalty": 1.05,
    "temperature": 0.7,
    "top_p": 1.0,
    "top_k": 30,
    "subtalker_dosample": true,
    "subtalker_temperature": 0.9,
    "subtalker_top_p": 1.0,
    "subtalker_top_k": 50,
    "max_new_tokens": 8192
}
```

Температура 0.7 вместо стандартной 0.9 – меньше срывов интонации и «писка» на эмоциональных фразах.

Модель 0.6B Base работает с той же скоростью и занимает 3,2 ГБ видеопамяти против 5,1 ГБ, но звучит заметно хуже на русском.

### 3. Конфиг сервера – `config-1.7B-t07.yaml`

```yaml
default_model: 1.7B-Base
models:
  1.7B-Base:
    hf_id: "D:/tts/qwen3/models/1.7B-t07"
    type: base
optimization:
  attention: sdpa
  use_compile: true
  compile_mode: default
  use_cuda_graphs: false
  use_fast_codebook: true
  compile_codebook_predictor: true
  streaming:
    decode_window_frames: 80
    emit_every_frames: 6
server:
  host: "127.0.0.1"
  port: 8880
voices: []
```

- `hf_id` – путь к каталогу, а не имя модели: иначе сервер обращается в интернет и падает без сети.
- `compile_mode: default`. `max-autotune` на Windows падает, CUDA graphs (`use_cuda_graphs: true`) не ускоряют и занимают до 8 ГБ видеопамяти.
- `host` перекрывает переменная `HOST` из `start-tts.ps1`.

### 4. Патч: предел длины генерации

Модель изредка не выдаёт маркер конца речи и генерирует до встроенного предела – 10 000 кадров, ~13 минут звука, ~20 минут работы. Сервер обрабатывает один запрос за раз (`TTS_MAX_CONCURRENT=1`), поэтому все следующие запросы ждут, и голос пропадает. Патч ограничивает генерацию длиной текста.

Файл `server\api\backends\optimized_backend.py`, метод `generate_voice_clone_streaming`, цикл в конце метода:

```python
        # A clone that misses its end-of-speech token keeps generating to max_frames
        # (10 000 frames, 13 min of audio) and holds the GPU for ~20 min, slowing every
        # request that runs beside it. Speech is ~12.5 frames/s at ~14 chars/s, so three
        # frames per character plus a margin covers any real answer
        max_frames = min(10000, 75 + 3 * len(text))
        frames_per_chunk = emit_every_frames
        emitted = 0
        for chunk, sr in self.model.stream_generate_voice_clone(
            text=text,
            language=language,
            voice_clone_prompt=prompt_items,
            emit_every_frames=emit_every_frames,
            decode_window_frames=decode_window_frames,
            max_frames=max_frames,
        ):
            emitted += frames_per_chunk
            yield chunk, sr
        if emitted >= max_frames - frames_per_chunk:
            logger.warning(
                f"Voice clone stream hit max_frames={max_frames} for {len(text)} chars: "
                f"the model missed its end of speech, output cut"
            )
```

Исходный цикл – тот же вызов без `max_frames`. Патч теряется при обновлении или переустановке сервера: после них его нужно внести заново. Исходная версия файла – `optimized_backend.py.orig` рядом.

### 5. Голосовой профиль

Профиль – каталог `voice_library\profiles\<имя>\` с двумя файлами. Бот обращается к нему как `clone:<имя>` (`VOICE_TTS_VOICE`).

`meta.json`:

```json
{
 "name": "kael_low",
 "profile_id": "kael_low",
 "ref_audio_filename": "reference.wav",
 "ref_text": "Я теряю терпение. Приказывай. Будет сделано. Сделаю всё, что смогу. Замечательно. Призраки Кельталаса взывают ко мне. Не стоит меня недооценивать. Время Альянса прошло.",
 "x_vector_only_mode": false,
 "language": "Russian"
}
```

`reference.wav` – образец голоса:

- 12–18 с чистой речи одного голоса, без музыки и фона; моно, 24 кГц; громкость около −18 dBFS;
- реплики разделены паузами ~0,35 с, края реплик сглажены (10 мс), чтобы не было щелчков;
- `ref_text` – **точная** расшифровка образца: при `x_vector_only_mode: false` модель копирует не только тембр, но и манеру, и опирается на текст. Ошибка в расшифровке даёт акцент и искажения;
- `language: Russian` обязателен: иначе интонация и ритм уходят в английские;
- темп и высота голоса берутся из образца. Профиль `kael_low` собран только из реплик с низким голосом (медиана F0 до 138 Гц): с высокими модель уходит в «писк» на эмоциональных фразах.

Новый профиль сервер видит без перезапуска.

Что не работает:

| Попытка | Результат |
|---|---|
| Образец с музыкой или фоном | Шум и артефакты в речи |
| Образец, ускоренный в 1,1–1,2 раза (librosa) | Речь короче лишь на ~4 %, звук заметно портится |
| Параметр `speed` в запросе | Действует только без потока (`stream: false`) |
| Дообучение на голосе | Нужны десятки минут чистой речи; нескольких минут образцов мало |

### 6. Запуск – `start-tts.ps1`

```powershell
[Console]::OutputEncoding=[Text.Encoding]::UTF8
$root='D:\tts\qwen3'
$env:TTS_BACKEND='optimized'; $env:TTS_CONFIG="$root\config-1.7B-t07.yaml"; $env:HOST='192.168.88.150'; $env:PORT='8880'
$env:TRITON_CACHE_DIR="$root\cache\triton"; $env:TORCHINDUCTOR_CACHE_DIR="$root\cache\inductor"; $env:TEMP="$root\cache\tmp"; $env:TMP=$env:TEMP
$env:TORCH_LOGS='recompiles'
$env:VOICE_LIBRARY_DIR="$root\voice_library"; $env:HF_HOME="$root\hf"; $env:HF_HUB_OFFLINE='1'; $env:PYTHONIOENCODING='utf-8'
New-Item -ItemType Directory -Force $env:TEMP, "$root\logs" | Out-Null
Set-Location "$root\server"
Start-Job -Name tts-warmup -ScriptBlock { <прогрев, см. ниже> } | Out-Null
& "$root\venv\Scripts\python.exe" -m api.main 2>&1 | Tee-Object -FilePath "$root\logs\server-prod.log"
```

`start-tts.bat` запускает этот скрипт в окне: `powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-tts.ps1"`. Файл `.ps1` с кириллицей сохраняется в UTF-8 **с BOM**, иначе Windows PowerShell 5 читает его в другой кодировке.

| Переменная | Назначение |
|---|---|
| `TTS_BACKEND=optimized` | Бэкенд с `torch.compile` и потоковой генерацией |
| `TTS_CONFIG` | Путь к конфигу; по умолчанию `~/qwen3-tts/config.yaml` |
| `HOST`, `PORT` | Адрес и порт; адрес Windows в локальной сети, чтобы бот из ВМ достучался |
| `HF_HOME`, `HF_HUB_OFFLINE=1` | Модели только из локального кеша, без обращений в интернет |
| `VOICE_LIBRARY_DIR` | Каталог голосовых профилей |
| `TRITON_CACHE_DIR`, `TORCHINDUCTOR_CACHE_DIR`, `TEMP` | Кеши компиляции внутри каталога сервера |
| `TORCH_LOGS=recompiles` | Каждая перекомпиляция `torch.compile` и её причина – в лог |
| `TTS_MAX_CONCURRENT` | Одновременных генераций, по умолчанию 1 – для одной видеокарты не менять |

**Прогрев.** Модель загружается на первом запросе (~40 с), а `torch.compile` строит вариант под каждую новую форму входа: первые запросы после старта идут медленнее речи. Фоновая задача `tts-warmup` ждёт, пока сервер ответит на `/health`, синтезирует три фразы разной длины (1, 6 и 16 слов) и ставит процессу сервера приоритет **High**. Прогрев занимает ~90 с; после него первый звук приходит за ~0,6 с. Задача живёт, пока открыто окно сервера.

**Приоритет.** Генерация – один поток Python, который подаёт видеокарте мелкие шаги. Когда на соседнем логическом потоке того же физического ядра работает другая нагрузка, генерация замедляется на ~20 %; высокий приоритет помогает планировщику держать поток на свободном ядре. Привязка процесса к одному ядру (`ProcessorAffinity`) в обычной работе выигрыша не даёт.

### 7. Остановка – `stop-tts.bat`

```bat
@echo off
powershell -NoProfile -Command "Get-NetTCPConnection -LocalPort 8880 -State Listen -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force; \"TTS stopped (PID $($_.OwningProcess))\" }"
timeout /t 3
```

В `.bat` символ `%` – переменная cmd: запись `%{ … }` вместо `ForEach-Object { … }` молча ломает команду. Закрыть окно сервера – то же самое.

## Работа

1. Перед стримом – запустить `start-tts.bat` и подождать ~1,5 минуты (прогрев).
2. Бот подключается сам: перезапускать его не нужно. Голосовой канал и включение голоса – команды `!join`, `!leave`, `!voice` в Discord (BOT.md, «Голос в Discord»).
3. После стрима – закрыть окно сервера: видеокарта освобождается.

Сервер не запускается со стартом Windows и не держит видеокарту, пока окно закрыто.

## API

```
POST http://<HOST>:8880/v1/audio/speech
Content-Type: application/json

{
  "model": "tts-1-ru",
  "voice": "clone:kael_low",
  "input": "Текст для озвучки.",
  "response_format": "pcm",
  "stream": true,
  "normalization_options": {"normalize": false}
}
```

- Ответ – поток 16-битного моно PCM 24 кГц (`response_format: pcm`, `stream: true`); для потока допустимы только `pcm` и `wav`.
- `model` – любое имя, которое сервер принимает: модель задаёт конфиг.
- `normalization_options.normalize: false` обязателен для русского. Нормализация сервера английская: `@` → «at», цифры – английскими числами, `_` → пробел; это и даёт «американский акцент». Текст готовит бот (`src/discord/local/voice/text.py`): ники – по словарю `lists.voice_nicks`, числа – русскими словами, эмоты и ссылки – вырезаются.
- Прочее: `GET /health` (и загрузка модели, если она ещё не загружена), `GET /v1/audio/voices` – список голосов, `GET /docs` – Swagger.

Проверка с машины бота:

```bash
curl -s -o /tmp/t.pcm -w '%{time_starttransfer}s до первого звука, %{time_total}s всего\n' \
  -H 'Content-Type: application/json' \
  -d '{"model":"tts-1-ru","voice":"clone:kael_low","input":"Проверка связи.","response_format":"pcm","stream":true,"normalization_options":{"normalize":false}}' \
  http://192.168.88.150:8880/v1/audio/speech
```

## Подключение бота

В `.env` бота:

| Переменная | Значение |
|---|---|
| `VOICE_TTS_URL` | `http://192.168.88.150:8880` – адрес сервера |
| `VOICE_TTS_VOICE` | `clone:kael_low` – профиль голоса |

Остальные `VOICE_*` и `DISCORD_*` – README, «Переменные окружения». Образ бота несёт libopus сам; при запуске вне контейнера нужна системная `libopus.so.0`.

## Производительность

RTF – секунд синтеза на секунду речи; меньше 1 – быстрее речи.

| Условие | Первый звук | RTF | Видеопамять |
|---|---|---|---|
| 1.7B, `torch.compile`, после прогрева | 0,5–0,7 с | 0,8–1,2 | 5,2–7 ГБ |
| То же, первые запросы без прогрева | – | 1,6–1,8 | – |
| Без `torch.compile` (нет triton) | – | ~1,6 | – |
| CPU в ВМ (8 ядер, модель 0.6B) | 1,5–2 с | 2–4 | – |

- Скорость колеблется с нагрузкой на машину: OBS, игра, браузер и соседние потоки на тех же ядрах дают разброс 0,8–1,3 после прогрева.
- Бот меряет скорость сервера на каждом ответе и копит перед началом речи столько, чтобы она не кончилась раньше генерации; не успела – одна пауза вместо треска (BOT.md, «Голос в Discord»).
- Строка в логе бота: `Озвучка: N симв., T с, запас P с, пауз K (X с), скорость сервера R`.
- Строка в логе сервера на каждый запрос: `Voice clone stream done: total=… audio=… RTF=…`.

## Диагностика

| Симптом | Причина | Что делать |
|---|---|---|
| В логе бота «TTS-сервер недоступен (Cannot connect…)» | Сервер не запущен | `start-tts.bat` |
| То же сразу после запуска сервера | Идёт прогрев (~90 с) | Подождать |
| Голос пропал на минуты, сервер отвечает на `/health` медленно | Генерация без конца речи держит сервер | Проверить, что патч из п. 4 на месте; перезапустить сервер |
| «Рация», паузы в длинных ответах | Сервер медленнее речи (RTF > 1,3) | Посмотреть `скорость сервера` в логе бота; закрыть тяжёлые программы; перезапустить сервер |
| Первые ответы после запуска медленные | Нет прогрева | Запускать через `start-tts.bat`, не `python -m api.main` напрямую |
| Акцент, английские числа, «at» вместо ника | Нормализация сервера включена | `normalize: false` в запросе |
| Искажения, чужая интонация | Неточный `ref_text` или фон в образце | Пересобрать профиль по п. 5 |
| Сервер пишет в интернет / падает без сети | `hf_id` – имя модели, а не путь | Путь к каталогу модели в конфиге |
| `stop-tts.bat` не останавливает | `%{ … }` в батнике | `ForEach-Object { … }` |

Где висит сервер – стек его процесса (py-spy в окружении сервера):

```powershell
$py = Get-NetTCPConnection -LocalPort 8880 -State Listen | Select-Object -First 1 -ExpandProperty OwningProcess
D:\tts\qwen3\venv\Scripts\py-spy.exe dump --pid $py
D:\tts\qwen3\venv\Scripts\py-spy.exe record --pid $py --duration 15 --format raw --output D:\tts\qwen3\cache\tmp\profile.txt
```

Генерация идёт в главном потоке сервера, внутри его цикла событий: пока модель считает, сервер почти не отвечает на другие запросы, даже на `/health`.

## Сеть и безопасность

- Сервер слушает адрес в локальной сети без авторизации – своей у него нет: любое устройство в сети может синтезировать речь любым профилем и занимать видеокарту. Ограничить доступ можно только снаружи – брандмауэром Windows, правилом на порт 8880 для адреса машины с ботом.
- Порт 8880 открыт, пока работает сервер. Когда окно закрыто, порт не слушается.
- Процессы, запущенные через SSH (`Start-Process`, фоновые команды), не завершаются при обрыве SSH-сессии: останавливать их явно, по PID.
