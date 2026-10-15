#!/usr/bin/env python3
"""Recreate the Android build rootfs from a pinned public Bloodborne Android release.

This downloads only the upstream APK, verifies its GitHub-published SHA-256, verifies the
embedded runtime.payload against assets/runtime.sha256, and extracts the rootfs plus the
game-free precompiled patch-parts shipped by that pinned runtime. No game executable or game
content is present in that release or extracted by this script.

The APK is retained locally as a reproducible source for the MIT display/input bridge
libraries used by build_frontend.sh.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VERSION = "0.8.4"
APK_URL = (
    "https://github.com/ducvd89/Bloodborne-Android/releases/download/"
    "android-0.8.4/Bloodborne-0.8.4.apk"
)
APK_SHA256 = "6b100906a25b55ba5ac38a9d8191178bda09955d0a469230a15a3de83f4104d9"
CACHE = ROOT / ".local-deps/android/upstream"
APK = CACHE / f"Bloodborne-{VERSION}.apk"
MIGRATION = ROOT / ".local-deps/android/migration/x"
ROOTFS = MIGRATION / "rootfs"
PATCH_PARTS = MIGRATION / "bbport/arm64/patch-parts"
DEVICE_LIBS = ROOT / ".local-deps/android/device-libs.txt"
PROVENANCE = ROOT / ".local-deps/android/upstream-runtime.json"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def download() -> None:
    CACHE.mkdir(parents=True, exist_ok=True)
    if APK.is_file() and sha256(APK) == APK_SHA256:
        return
    partial = APK.with_suffix(APK.suffix + ".part")
    partial.unlink(missing_ok=True)
    print(f"Downloading pinned upstream Android {VERSION} release...")
    with urllib.request.urlopen(APK_URL, timeout=120) as response, partial.open("wb") as out:
        shutil.copyfileobj(response, out, length=1024 * 1024)
    actual = sha256(partial)
    if actual != APK_SHA256:
        partial.unlink(missing_ok=True)
        raise SystemExit(f"APK SHA-256 mismatch: expected {APK_SHA256}, got {actual}")
    partial.replace(APK)


def safe_runtime_members(tar: tarfile.TarFile):
    """Select only reproducible, game-free inputs needed by our Android build."""
    for member in tar.getmembers():
        name = member.name.lstrip("./")
        if (name == "rootfs" or name.startswith("rootfs/") or
                name == "bbport/arm64/patch-parts" or
                name.startswith("bbport/arm64/patch-parts/")):
            yield member


def main() -> None:
    download()
    if sha256(APK) != APK_SHA256:
        raise SystemExit("Cached APK failed SHA-256 verification")

    with tempfile.TemporaryDirectory(prefix="bb-runtime-") as temporary:
        temp = Path(temporary)
        payload = temp / "runtime.tar.gz"
        with zipfile.ZipFile(APK) as z:
            names = set(z.namelist())
            required = {
                "assets/runtime.payload",
                "assets/runtime.sha256",
                "lib/arm64-v8a/libfxdisplay.so",
                "lib/arm64-v8a/libfxio.so",
            }
            missing = required - names
            if missing:
                raise SystemExit(f"Pinned APK is missing: {sorted(missing)}")
            expected_runtime = z.read("assets/runtime.sha256").decode("ascii").strip()
            if not expected_runtime or len(expected_runtime) != 64:
                raise SystemExit("Pinned APK has an invalid runtime.sha256")
            with z.open("assets/runtime.payload") as src, payload.open("wb") as dst:
                shutil.copyfileobj(src, dst, length=1024 * 1024)

        actual_runtime = sha256(payload)
        if actual_runtime != expected_runtime:
            raise SystemExit(
                f"runtime.payload SHA-256 mismatch: expected {expected_runtime}, got {actual_runtime}"
            )

        if ROOTFS.exists():
            shutil.rmtree(ROOTFS)
        MIGRATION.mkdir(parents=True, exist_ok=True)
        with tarfile.open(payload, "r:gz") as tar:
            members = list(safe_runtime_members(tar))
            if not members:
                raise SystemExit("runtime.payload contains no rootfs")
            try:
                tar.extractall(MIGRATION, members=members, filter="data")
            except TypeError:
                # Python < 3.12: safe because both APK and payload are pinned by SHA-256,
                # and only members under rootfs/ are selected above.
                tar.extractall(MIGRATION, members=members)

    required_rootfs = [
        ROOTFS / "usr/bin/FEX",
        ROOTFS / "usr/bin/Xvfb",
        ROOTFS / "usr/lib/aarch64-linux-gnu/libc.so.6",
        ROOTFS / "usr/lib/aarch64-linux-gnu/libvulkan_freedreno.so",
    ]
    missing = [str(p.relative_to(ROOT)) for p in required_rootfs if not p.exists()]
    if missing:
        raise SystemExit(f"Extracted rootfs is incomplete: {missing}")
    if not PATCH_PARTS.is_dir() or not any(PATCH_PARTS.glob("*.bin")):
        raise SystemExit("Pinned runtime contains no precompiled patch-parts")

    libs = set()
    for directory in (ROOTFS / "usr/lib/aarch64-linux-gnu", ROOTFS / "usr/lib"):
        if directory.is_dir():
            for path in directory.rglob("*"):
                if path.is_file() or path.is_symlink():
                    if ".so" in path.name:
                        libs.add(path.name)
    DEVICE_LIBS.parent.mkdir(parents=True, exist_ok=True)
    DEVICE_LIBS.write_text("\n".join(sorted(libs)) + "\n")

    provenance = {
        "upstreamRepository": "ducvd89/Bloodborne-Android",
        "release": f"android-{VERSION}",
        "apkUrl": APK_URL,
        "apkSha256": APK_SHA256,
        "runtimeSha256": expected_runtime,
        "rootfs": str(ROOTFS.relative_to(ROOT)),
        "patchParts": str(PATCH_PARTS.relative_to(ROOT)),
        "deviceLibs": str(DEVICE_LIBS.relative_to(ROOT)),
        "bridgeApk": str(APK.relative_to(ROOT)),
    }
    PROVENANCE.write_text(json.dumps(provenance, indent=2) + "\n")
    print(f"Rootfs: {ROOTFS}")
    print(f"Patch parts: {PATCH_PARTS}")
    print(f"Device libraries: {DEVICE_LIBS} ({len(libs)} names)")
    print(f"Bridge APK: {APK}")
    print(f"Runtime SHA-256: {expected_runtime}")


if __name__ == "__main__":
    main()
