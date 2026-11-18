# Разработка и сборка

Для работы на колонке используется подготовленная ARM64-среда Python 3.11. На компьютере разработчика достаточно Python 3.11+, для сборки Windows-установщика нужна Windows.

## Работа с кодом

```powershell
python -m pip install -e ".[build]"
$env:PYTHONPATH="src"
python -m unittest discover -s tests
```

`app.py` координирует голосовой цикл. `wake.py`, `recognizer.py`, `speech.py` и `audio.py` отвечают за звук; `conversations.py` — за истории; `openai_client.py`, `auth.py`, `vpn.py` — за подключения. Веб-сервер работает в потоке помощника, использует стандартную библиотеку Python, HTML/CSS/JS без веб-фреймворка, npm и CDN.

## Сборка релиза

1. Подготовьте совместимые ARM64-архивы `runtime.tar.gz` и `sherpa.tar.gz`. В рабочей установке они находятся в `/data/alisa/`. Экспортируйте только эти архивы, без токенов, VPN и истории. Версии компонентов и исходники указаны в `third_party`.
2. Создайте звуки и кешированные инструкции на компьютере с интернетом. Скрипт ничего не воспроизводит:

   ```powershell
   python tools/prepare_assets.py
   ```

3. Соберите установочный комплект:

   ```powershell
   python tools/build_release.py --runtime path/to/runtime.tar.gz --sherpa path/to/sherpa.tar.gz --xray path/to/xray-arm64.gz
   ```

4. Для самостоятельного `.exe` укажите папку официальных Android platform-tools:

   ```powershell
   python tools/build_windows.py --adb-dir path/to/platform-tools
   ```

В `dist` появятся комплект для колонки и Windows-установщик. Архивы исходников GitHub создаёт автоматически. Модель «Алиса» в них не копируется: установщик скачивает закреплённый upstream-файл и проверяет SHA256. Для установки без скачивания можно передать проверенный файл через `--wake-model path/to/alisa_mww.tflite`.

Бинарная ARM64-среда пока является отдельным входом сборки: её полная пересборка из исходников не автоматизирована. Не заменяйте отдельную библиотеку Sherpa без проверки совместимости с ctypes-структурами в `recognizer.py`.

## Локальная установка без готового exe

```powershell
python tools/install.py dist/AliceForMarusia-capsule-rk3308.zip --adb "C:\path\to\adb.exe"
```

На Linux/macOS используйте свой `adb`. Для выбора устройства передайте `--serial` с точным идентификатором из `adb devices`. Если настроен SSH по ключу:

```powershell
python tools/install.py dist/AliceForMarusia-capsule-rk3308.zip --host 192.168.1.42
```

Установщик не получает root и не прошивает разделы. Он проверяет платформу, место и контрольные суммы. Перед заменой сохраняет прежние файлы в `local-install-backup`, при ошибке пытается их восстановить. Авторизация и диалоги сохраняются.

## Диагностика

```sh
/etc/init.d/S99alisa restart
tail -n 40 /data/alisa/events.log
df -h /data /tmp
free
```

После загрузки ожидаются `MODEL_READY` и `KWS_READY`. Журнал ограничен тремя файлами по 256 КиБ; текст реплик скрывается. Временный `/tmp/alisa.log` может содержать речь — просмотрите и очистите его перед отправкой.

Для удаления помощника сначала остановите службу и сохраните нужные личные данные. Удаляйте только его файлы, не весь `/data/overlay`. Root и восстановление прошивки — отдельная процедура capsula-jb с собственным бэкапом колонки.
