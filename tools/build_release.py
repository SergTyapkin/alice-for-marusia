"""Create the RK3308 installation payload. GitHub supplies source archives."""

import argparse
import hashlib
import json
from pathlib import Path
import tarfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.1.1"


def cleaned_runtime(source, destination):
    """Keep dependency files, excluding application code and personal assets.

    The wake model is downloaded from its pinned upstream URL by the installer;
    it is not redistributed because upstream does not specify a model license.
    """
    allowed = ("python/", "glibc/", "mpg/", "edge/")
    with tarfile.open(source, "r:gz") as src, tarfile.open(destination, "w:gz") as dst:
        for member in src:
            name = member.name.removeprefix("./")
            if not (name.startswith(allowed) or name == "wake"):
                continue
            if (
                ".." in Path(name).parts
                or name.startswith("/")
                or member.isdev()
                or member.isfifo()
            ):
                raise ValueError("Unsafe runtime member")
            if "__pycache__" in name or name.endswith((".pyc", ".log")):
                continue
            member.name = name
            member.uid = member.gid = member.mtime = 0
            member.uname = member.gname = ""
            dst.addfile(member, src.extractfile(member) if member.isfile() else None)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path)
    parser.add_argument("--sherpa", type=Path)
    parser.add_argument("--xray", type=Path)
    parser.add_argument("--assets", type=Path, default=ROOT / "build/assets")
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    if not args.runtime or not args.sherpa:
        parser.error("--runtime and --sherpa are required")
    cleaned = ROOT / "build/runtime.tar.gz"
    cleaned.parent.mkdir(exist_ok=True)
    cleaned_runtime(args.runtime, cleaned)
    files = {"runtime.tar.gz": cleaned, "sherpa.tar.gz": args.sherpa}
    if args.xray:
        files["xray.gz"] = args.xray
    for path in (ROOT / "src/alice_for_marusia").rglob("*"):
        if path.is_file() and "__pycache__" not in path.parts:
            files[
                "app/alice_for_marusia/"
                + path.relative_to(ROOT / "src/alice_for_marusia").as_posix()
            ] = path
    for path in args.assets.rglob("*"):
        if path.is_file() and path.suffix in (".wav", ".mp3"):
            files[path.relative_to(args.assets).as_posix()] = path
    required = ["assets/" + name + ".wav" for name in ("wake", "ready", "error")]
    required += [
        "phrases-cache/setup-" + name + ".mp3" for name in ("offline", "login", "vpn")
    ]
    required += [
        "phrases-cache/digit-" + name + ".mp3"
        for name in [*(str(i) for i in range(10)), "dot", "colon"]
    ]
    if any(name not in files for name in required):
        raise ValueError("Run tools/prepare_assets.py first")
    files.update(
        {
            "S99alisa": ROOT / "deploy/S99alisa",
            "S98xray": ROOT / "deploy/S98xray",
            "alisa_mww_fast.json": ROOT / "config/wake-word.json",
        }
    )
    manifest = {
        "version": VERSION,
        "target": "rk3308-big",
        "sha256": {},
        "wake_model": json.loads(
            (ROOT / "config/wake-model-download.json").read_text()
        ),
    }
    bundle = args.output / "AliceForMarusia-capsule-rk3308.zip"
    with zipfile.ZipFile(bundle, "w", zipfile.ZIP_STORED) as archive:
        for name, path in files.items():
            data = path.read_bytes()
            if name in ("S98xray", "S99alisa"):
                data = data.replace(b"\r\n", b"\n")
            archive.writestr(name, data)
            manifest["sha256"][name] = sha256(data)
        archive.writestr("manifest.json", json.dumps(manifest, indent=2))
        for name in ("LICENSE", "THIRD_PARTY.md"):
            archive.write(ROOT / name, name)
        for path in (ROOT / "third_party").rglob("*"):
            if path.is_file():
                archive.write(path, path.relative_to(ROOT).as_posix())
    print("Installation archive created")


if __name__ == "__main__":
    main()
