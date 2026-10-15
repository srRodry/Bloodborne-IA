#!/usr/bin/env bash
# Build a Bloodborne-only Android UI using the installed runtime's native bridge ABI.
set -euo pipefail
cd -- "$(dirname -- "$0")/../.."
sdk=${BB_ANDROID_SDK:-"$PWD/.local-deps/android/sdk"}
build_tools="$sdk/build-tools/35.0.0"
android_jar="$sdk/platforms/android-35/android.jar"
# Source APK for the MIT display/input bridge libraries. The reproducible bootstrap keeps the
# pinned upstream Bloodborne Android 0.8.4 APK here. BB_FEXDROID_APK remains as a compatibility
# alias for older local build environments.
bridge_apk=${BB_BRIDGE_APK:-${BB_FEXDROID_APK:-"$PWD/.local-deps/android/upstream/Bloodborne-0.8.4.apk"}}
bridge_sha=${BB_BRIDGE_APK_SHA256:-}
local_jdk="$PWD/.local-deps/android/jdk/usr/lib/jvm/java-27-openjdk"
if [[ -x $local_jdk/bin/javac ]]; then
    export JAVA_HOME="$local_jdk" PATH="$local_jdk/bin:$PATH"
fi
require_cmd() {
    command -v "$1" >/dev/null 2>&1 || { echo "Missing required host tool: $1" >&2; exit 1; }
}
for cmd in python3 javac keytool sed find sort cp grep; do require_cmd "$cmd"; done
[[ -f $android_jar ]] || { echo "Missing Android platform jar: $android_jar" >&2; exit 1; }
for tool in aapt d8 zipalign apksigner; do
    [[ -x $build_tools/$tool ]] || { echo "Missing Android build tool: $build_tools/$tool" >&2; exit 1; }
done
out="$PWD/out/thor/frontend"
rm -rf "$out"
mkdir -p "$out/classes" "$out/dex" "$out/res/drawable" "$out/assets"
cp -r tools/android/frontend/res/. "$out/res/"
if [[ -n ${BB_RUNTIME_ARCHIVE:-} ]]; then
    runtime_sha="${BB_RUNTIME_ARCHIVE%.tar.gz}.sha256"
    runtime_packages="${BB_RUNTIME_ARCHIVE%.tar.gz}.python-packages.txt"
    for input in "$BB_RUNTIME_ARCHIVE" "$runtime_sha" "$runtime_packages"; do
        [[ -s $input ]] || { echo "Missing runtime build input: $input" >&2; exit 1; }
    done
    cp "$BB_RUNTIME_ARCHIVE" "$out/assets/runtime.payload"
    cp "$runtime_sha" "$out/assets/runtime.sha256"
    cp "$runtime_packages" "$out/assets/python-packages.txt"
fi
# Use the user's own Bloodborne icon locally; game art is not committed to this repository.
python3 - "$bridge_apk" "$out" "$bridge_sha" <<'PY'
import hashlib, sys, zipfile
from pathlib import Path
apk, out, requested = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3].strip()
if not apk.is_file():
    raise SystemExit(f'Missing bridge APK: {apk}. Run tools/android/bootstrap_release_rootfs.py first.')
actual = hashlib.file_digest(apk.open('rb'), 'sha256').hexdigest()
# Known reproducible inputs: upstream Bloodborne Android 0.8.4, or the historical fexdroid APK.
allowed = {
    '6b100906a25b55ba5ac38a9d8191178bda09955d0a469230a15a3de83f4104d9',
    '2074fcee551dab0c6b0719c3f90f6389e517f194aaabacb5b0080bb2977ed847',
}
if requested:
    if actual != requested:
        raise SystemExit(f'Bridge APK SHA-256 mismatch: expected {requested}, got {actual}')
elif actual not in allowed:
    raise SystemExit(f'Unpinned bridge APK SHA-256: {actual}')
# A local game icon may override the source fallback, but release builds never require game assets.
icon = Path('game/CUSA03173/sce_sys/icon0.dds')
if icon.exists():
    try:
        from PIL import Image
    except ImportError as exc:
        raise SystemExit('Pillow is required only when using a local DDS game icon') from exc
    (out/'res/drawable/icon.xml').unlink(missing_ok=True)
    Image.open(icon).save(out/'res/drawable/icon.png')
with zipfile.ZipFile(apk) as z:
    for name in ('lib/arm64-v8a/libfxdisplay.so', 'lib/arm64-v8a/libfxio.so'):
        if name not in z.namelist():
            raise SystemExit(f'Bridge APK does not contain {name}')
        z.extract(name, out)
PY
cp tools/android/frontend/THIRD_PARTY_LICENSES.txt "$out/assets/"
# The Steam sign-in's libraries (SteamDllDialog.java: JavaSteam and its dependencies, ZXing, zstd).
steam_libs=$PWD/.local-deps/android/steam-libs
if [[ ! -s $steam_libs/jars.txt ]] ||
   ! find "$steam_libs/jni" -maxdepth 1 -type f -name '*.so' -print -quit 2>/dev/null | grep -q .; then
    bash tools/android/fetch_steam_libs.sh
fi
mapfile -t steam_jars < <(sed "s|^|$steam_libs/jars/|" "$steam_libs/jars.txt")
((${#steam_jars[@]} > 0)) || { echo 'No Steam dependency jars were resolved' >&2; exit 1; }
for jar in "${steam_jars[@]}"; do
    [[ -s $jar ]] || { echo "Missing Steam dependency jar: $jar" >&2; exit 1; }
done
mkdir -p "$out/lib/arm64-v8a"
mapfile -d '' -t jni_libs < <(find "$steam_libs/jni" -maxdepth 1 -type f -name '*.so' -print0 | sort -z)
((${#jni_libs[@]} > 0)) || { echo 'No ARM64 Steam JNI libraries were resolved' >&2; exit 1; }
cp "${jni_libs[@]}" "$out/lib/arm64-v8a/"
mapfile -d '' -t sources < <(find tools/android/frontend/src -type f -name '*.java' -print0 | sort -z)
((${#sources[@]} > 0)) || { echo 'No Android frontend Java sources found' >&2; exit 1; }
javac --release 8 -classpath "$android_jar:$(IFS=:; echo "${steam_jars[*]}")" -d "$out/classes" "${sources[@]}"
mapfile -d '' -t classes < <(find "$out/classes" -type f -name '*.class' -print0 | sort -z)
((${#classes[@]} > 0)) || { echo 'javac produced no class files' >&2; exit 1; }
"$build_tools/d8" --release --min-api 28 --lib "$android_jar" --output "$out/dex" "${classes[@]}" "${steam_jars[@]}"
"$build_tools/aapt" package -f -M tools/android/frontend/AndroidManifest.xml \
    -S "$out/res" -A "$out/assets" -I "$android_jar" -0 payload -F "$out/unsigned.apk"
python3 - "$out" <<'PY'
import sys, zipfile
from pathlib import Path
out = Path(sys.argv[1])
with zipfile.ZipFile(out/'unsigned.apk','a',compression=zipfile.ZIP_DEFLATED) as z:
    for p in sorted((out/'dex').glob('*.dex')): z.write(p,p.name)
    for p in sorted((out/'lib').rglob('*.so')): z.write(p,str(p.relative_to(out)))
PY
"$build_tools/zipalign" -f 4 "$out/unsigned.apk" "$out/aligned.apk"
key="$PWD/.local-deps/android/bloodborne-thor.keystore"
if [[ ! -f $key ]]; then
    keytool -genkeypair -keystore "$key" -storepass android -keypass android \
        -alias thor -dname 'CN=Bloodborne Thor local build' -keyalg RSA -keysize 2048 -validity 10000
fi
"$build_tools/apksigner" sign --ks "$key" --ks-pass pass:android --key-pass pass:android \
    --out "$out/Bloodborne-Thor.apk" "$out/aligned.apk"
"$build_tools/apksigner" verify --verbose "$out/Bloodborne-Thor.apk"
echo "Built $out/Bloodborne-Thor.apk"
