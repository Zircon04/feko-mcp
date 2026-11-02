# FEKO MCP

[English documentation](README.md)

Локальный MCP-сервер для цепочки **CADFEKO → решатель FEKO → POSTFEKO**.
ИИ может строить модель через Lua API, запускать расчёт, проверять состояние
и получать S11, входное сопротивление, Touchstone и изображения.

Проверено на **Windows, FEKO 2026.1, Python 3.11+**. Другие версии и ОС пока
не проверены. Нужны собственная установка FEKO и подходящая лицензия.
Это независимая интеграция, не официальный продукт Altair.

## Установка

```powershell
git clone https://github.com/Zircon04/feko-mcp.git
cd feko-mcp
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install .
$env:FEKO_HOME = 'C:\Program Files\Altair\2026.1'
$env:FEKO_MCP_WORKSPACE = "$PWD\jobs"
.\.venv\Scripts\feko-mcp.exe doctor
```

Указать свои пути. `FEKO_HOME` — каталог версии с вложенным `feko/bin`
либо сам каталог `feko`. Если программы есть в PATH, путь определяется
автоматически. `doctor` проверяет программы; лицензию проверяет FEKO при расчёте.

## Подключение к Codex

Добавить в `config.toml`, заменив пути на свои:

```toml
[mcp_servers.feko]
command = 'C:\path\to\feko-mcp\.venv\Scripts\python.exe'
args = ['-m', 'feko_mcp']
startup_timeout_sec = 20

[mcp_servers.feko.env]
FEKO_HOME = 'C:\Program Files\Altair\2026.1'
FEKO_MCP_WORKSPACE = 'C:\path\to\feko-jobs'
```

Перезапустить подключение MCP в настройках клиента.
[Официальная инструкция OpenAI](https://learn.chatgpt.com/docs/extend/mcp).
Для клиентов с `mcpServers` есть [пример JSON](mcp-config.example.json).
Переменные из терминала не появляются автоматически в открытом приложении:
задавать их в конфигурации MCP. Серверу ключ OpenAI API не нужен.

## Работа

1. Проверить `get_environment`, прочитать API через `read_api`.
2. Передать Lua в `run_cad_script` или вызвать `create_preliminary_model`.
3. Дождаться `build=completed` через `get_job_status`.
4. Запустить `start_solver`, дождаться завершения `solve`.
5. Вызвать `run_post_script`, проверить стадию `post`.
6. Получить файлы через `get_artifacts`, данные через `read_result_text`.

Для встроенного экспорта S11, входного сопротивления первого возбуждения и
изображений оставить `script_text` пустым в `run_post_script`, а в `model_base`
указать имя рассчитанной модели без расширения. В собственном скрипте перед
экспортом 3D PNG явно активировать нужный вид через `view:Show()`, затем задать
размер окна и камеру. При нативном тестировании это повысило надёжность экспорта:

```lua
local view = app.Views[1]
view:Show()
view:Restore(); view:SetSize(800,800)
view:SetViewDirection(pf.Enums.ViewDirectionEnum.Isometric)
view:ZoomToExtents()
view:ExportImage("model_isometric", "png", 1200,1000)
```

Встроенный экспорт сохраняет прежние настройки масштаба камеры и записывает
для каждого PNG сообщения `EXPORT_IMAGE_START` / `EXPORT_IMAGE_DONE` в `post.log`
с фактической длительностью `elapsed_seconds`, с разрешением в секунду
(быстрый экспорт может дать ноль). Сообщения доступны и в конце журнала через
`get_job_status`. `EXPORT_COMPLETE` выводится после сохранения PFS; также
проверять успешное завершение стадии. Собственные Lua-скрипты передаются без изменений.

Сохранять `job_id` и проверять завершение, код возврата и сообщения FEKO.
Задания находятся в `<workspace>/jobs/<job_id>` и продолжаются в отдельных
процессах после возврата управления. Встроенной отмены нет: перед ручным
управлением процессом проверить его PID и состояние.

Lua выполняется с правами пользователя. Использовать доверенные скрипты
и клиенты; сервер не изолирует произвольный код и не открывает сетевой порт.

## Пример без ИИ

```powershell
.\.venv\Scripts\feko-mcp.exe example build --smoke
.\.venv\Scripts\feko-mcp.exe example solve --smoke --cores 1
.\.venv\Scripts\feko-mcp.exe example post --smoke
```

Это проверка на 2,4 ГГц. Для 1–10 ГГц убрать `--smoke` во всех трёх командах.
Файлы — в `<workspace>/example`; менять его `parameters.lua` перед перестроением.

Антенна — **предварительная реконструкция с допущениями**, не проверенный
оптимум. [Допущения и источник](src/feko_mcp/examples/MODEL_NOTES.md).
Характеристики исходной антенны не воспроизведены, сходимость сетки не проверена.
Исходные PDF и файлы Altair в репозитории отсутствуют.

Все настройки, инструменты и команды проверки — в [English README](README.md).
Код сервера распространяется по MIT.
