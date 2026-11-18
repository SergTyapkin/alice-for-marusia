"""Build a self-contained Windows installer with the payload and ADB."""

import argparse
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adb-dir", type=Path, required=True)
    parser.add_argument(
        "--bundle", type=Path, default=ROOT / "dist/AliceForMarusia-capsule-rk3308.zip"
    )
    args = parser.parse_args()
    if sys.platform != "win32":
        parser.error("Build the Windows executable on Windows")
    assets = [(args.bundle, "payload")]
    for name in ("adb.exe", "AdbWinApi.dll", "AdbWinUsbApi.dll"):
        path = args.adb_dir / name
        if not path.exists():
            parser.error("Missing platform-tools file: " + str(path))
        assets.append((path, "platform-tools"))
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--console",
        "--name",
        "AliceForMarusia-Setup",
        "--distpath",
        str(ROOT / "dist"),
        "--workpath",
        str(ROOT / "build/pyinstaller"),
        "--specpath",
        str(ROOT / "build"),
    ]
    for source, dest in assets:
        command += ["--add-data", str(source.resolve()) + ":" + dest]
    command.append(str(ROOT / "tools/install.py"))
    subprocess.run(command, check=True)


if __name__ == "__main__":
    main()
