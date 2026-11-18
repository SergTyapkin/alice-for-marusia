"""Install Alice on an already-rooted large Capsula over ADB or SSH."""

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import urllib.request
import zipfile

VERSION = "0.1.1"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def resources():
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))


def load_bundle(path):
    files = {}
    with zipfile.ZipFile(path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        if manifest.get("target") != "rk3308-big":
            raise ValueError("Пакет предназначен для другой модели колонки.")
        for name, checksum in manifest["sha256"].items():
            item = PurePosixPath(name)
            if (
                item.is_absolute()
                or ".." in item.parts
                or any(
                    c
                    not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-/"
                    for c in name
                )
            ):
                raise ValueError("Небезопасный путь в установочном пакете.")
            data = archive.read(name)
            if digest(data) != checksum:
                raise ValueError("Не совпала контрольная сумма: " + name)
            files[name] = data
    return manifest, files


def find_adb(supplied):
    if supplied:
        return supplied
    bundled = resources() / "platform-tools/adb.exe"
    candidate = str(bundled) if bundled.exists() else shutil.which("adb")
    if not candidate:
        raise ValueError(
            "ADB не найден. Укажите --adb PATH или используйте готовый .exe."
        )
    return candidate


def choose_device(adb, serial):
    if serial:
        return serial
    output = subprocess.run(
        [adb, "devices"], capture_output=True, check=True, text=True
    ).stdout
    devices = [
        line.split()[0]
        for line in output.splitlines()[1:]
        if len(line.split()) == 2 and line.split()[1] == "device"
    ]
    if not devices:
        raise ValueError(
            "Колонка не найдена. Подключите USB после получения root и проверьте adb devices."
        )
    if len(devices) == 1:
        return devices[0]
    for i, name in enumerate(devices, 1):
        print(f"{i}. {name}")
    selected = int(input("Номер колонки из списка: "))
    if not 1 <= selected <= len(devices):
        raise ValueError("Неверный номер устройства.")
    return devices[selected - 1]


def destination(name):
    if name in ("S99alisa", "S98xray"):
        return "/etc/init.d/" + name
    if name == "xray.gz":
        return "/data/xray-test/xray.gz"
    return "/data/alisa/" + name


def install(args):
    manifest, files = load_bundle(args.bundle)
    if args.verify_bundle:
        print(f'Пакет {manifest["version"]}: проверено {len(files)} файлов.')
        return
    if args.host:
        if args.host.startswith("-") or any(
            c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-:"
            for c in args.host
        ):
            raise ValueError("Некорректный IP/имя SSH-хоста.")
        transport = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=8",
            "root@" + args.host,
        ]
    else:
        adb = find_adb(args.adb)
        transport = [adb, "-s", choose_device(adb, args.serial), "shell"]

    def shell(command, data=None):
        result = subprocess.run(
            transport + [command], input=data, capture_output=True, timeout=180
        )
        if result.returncode:
            raise RuntimeError(
                "Ошибка команды на колонке. Проверьте соединение и свободное место."
            )
        return result.stdout

    machine = shell("uname -m; cat /proc/device-tree/compatible; id -u")
    if (
        b"aarch64" not in machine
        or b"rk3308" not in machine.lower()
        or not machine.strip().endswith(b"0")
    ):
        raise ValueError(
            "Нужна большая Капсула RK3308 с root. Мини этим установщиком не поддерживается."
        )
    model = manifest["wake_model"]
    if args.wake_model:
        model_data = args.wake_model.read_bytes()
    else:
        print("Получаем модель «Алиса» из исходного репозитория…", flush=True)
        request = urllib.request.Request(
            model["url"], headers={"User-Agent": "AliceForMarusia-Installer/" + VERSION}
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            model_data = response.read(2 * 1024 * 1024)
    if digest(model_data) != model["sha256"]:
        raise ValueError("Не совпала контрольная сумма модели слова активации.")
    files["alisa_mww.tflite"] = model_data
    free = int(shell("df -k /data | tail -n 1 | awk '{print $4}'").strip()) * 1024
    existing = (
        int(
            shell(
                "du -sk /data/alisa/runtime.tar.gz /data/alisa/sherpa.tar.gz /data/xray-test/xray.gz 2>/dev/null | awk '{s+=$1} END {print s+0}'"
            ).strip()
        )
        * 1024
    )
    if free + existing < sum(map(len, files.values())) + 3 * 1024 * 1024:
        raise ValueError(
            "Недостаточно места в /data. Сохраните и уберите ненужные сторонние пакеты вручную."
        )
    shell(
        "umask 077; mkdir -p /data/alisa /data/xray-test; if [ -f /etc/init.d/S99alisa ]; then /etc/init.d/S99alisa stop || exit 1; fi"
    )
    backup = Path("local-install-backup")
    backup.mkdir(exist_ok=True)
    changed = []
    try:
        for index, (name, data) in enumerate(files.items(), 1):
            dest = destination(name)
            try:
                old_hash = shell("sha256sum " + dest).decode().split()[0]
            except RuntimeError:
                old_hash = None
            if old_hash == digest(data):
                continue
            print(f"[{index}/{len(files)}] {name}", flush=True)
            if old_hash:
                previous = shell("cat " + dest)
                if digest(previous) != old_hash:
                    raise RuntimeError(
                        "Не удалось проверить резервную копию прежнего файла."
                    )
                local = backup / dest.lstrip("/")
                local.parent.mkdir(parents=True, exist_ok=True)
                local.write_bytes(previous)
            changed.append((dest, old_hash is not None))
            if old_hash and name in ("runtime.tar.gz", "sherpa.tar.gz", "xray.gz"):
                shell("rm " + dest)
            parent = dest.rsplit("/", 1)[0]
            shell("umask 077; mkdir -p " + parent + "; cat > " + dest + ".new", data)
            if shell("sha256sum " + dest + ".new").decode().split()[0] != digest(data):
                raise RuntimeError("Не совпала контрольная сумма переданного файла.")
            shell(
                "mv "
                + dest
                + ".new "
                + dest
                + ("; chmod 755 " + dest if name in ("S98xray", "S99alisa") else "")
            )
        shell("/etc/init.d/S99alisa start")
    except Exception:
        print("Установка не завершена. Восстанавливаем изменённые файлы…", flush=True)
        for dest, had_previous in reversed(changed):
            try:
                if had_previous:
                    shell("cat > " + dest, (backup / dest.lstrip("/")).read_bytes())
                    if dest.startswith("/etc/init.d/"):
                        shell("chmod 755 " + dest)
                else:
                    shell("rm -f " + dest + " " + dest + ".new")
            except Exception:
                print(
                    "Не удалось восстановить "
                    + dest
                    + ". Копия сохранена на компьютере."
                )
        raise
    print(
        "\nУстановлено. Дождитесь сигнала готовности. Откройте http://IP_КОЛОНКИ:100/."
    )
    print(
        shell("ip -4 addr show wlan0 2>/dev/null || ifconfig wlan0").decode(
            errors="replace"
        )
    )


def main():
    embedded = resources() / "payload/AliceForMarusia-capsule-rk3308.zip"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "bundle", nargs="?", type=Path, default=embedded if embedded.exists() else None
    )
    parser.add_argument("--version", action="version", version=VERSION)
    transport = parser.add_mutually_exclusive_group()
    transport.add_argument("--host", help="IP колонки для SSH по ключу")
    transport.add_argument(
        "--adb", help="Путь к adb.exe; готовый установщик содержит ADB"
    )
    parser.add_argument(
        "--serial", help="Точный идентификатор из adb devices, не COM-порт"
    )
    parser.add_argument(
        "--list-devices",
        action="store_true",
        help="Показать устройства ADB без установки",
    )
    parser.add_argument(
        "--wake-model",
        type=Path,
        help="Локальная проверенная модель для установки без скачивания",
    )
    parser.add_argument(
        "--verify-bundle",
        action="store_true",
        help="Проверить комплект без подключения к колонке",
    )
    args = parser.parse_args()
    if args.list_devices:
        subprocess.run([find_adb(args.adb), "devices"], check=True)
        return 0
    if not args.bundle:
        parser.error(
            "Укажите установочный ZIP или используйте готовый .exe из Releases."
        )
    try:
        install(args)
        return 0
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as error:
        print("Ошибка: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    result = main()
    if getattr(sys, "frozen", False) and len(sys.argv) == 1 and sys.stdin.isatty():
        input("\nНажмите Enter, чтобы закрыть окно…")
    raise SystemExit(result)
