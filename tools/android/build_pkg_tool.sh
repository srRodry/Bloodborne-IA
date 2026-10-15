#!/usr/bin/env bash
# ps4-pkg-tool (github.com/xXJSONDeruloXx/ps4-pkg-tools at 45baeda: shadPS4's PKG/PFS code, GPL-2.0),
# the CLI that unpacks PS4 fake PKGs for the app's mod installer (mods_android.py install-pkg).
# Built out of tree from the (read-only) source:
#   arm64 -> out/arm64/pkg-tool/ps4-pkg-tool  (also copied to out/arm64/bundle/bin/ when that exists;
#            build_release_runtime.py bundles it as bbport/arm64/bin/ps4-pkg-tool)
#   host  -> out/host/pkg-tool/ps4-pkg-tool   (tests/test_mods_android.py)
# Usage: tools/android/build_pkg_tool.sh [arm64|host|all]   (default: all)
# Our patches (tools/android/pkg-tool-patches: byte progress, 64-bit offsets for multi-GB PKGs) are
# applied to a copy of the source in out/pkg-tool-src; the original is never touched.
# Source: $BB_PKG_TOOL_SRC, else .local-deps/src/ps4-pkg-tools (here or in the main checkout).
set -euo pipefail
cd -- "$(dirname -- "$0")/../.."
root=$PWD
which=${1:-all}
src=${BB_PKG_TOOL_SRC:-}
if [[ -z $src ]]; then
    common=$(cd -- "$(git rev-parse --git-common-dir)/.." && pwd)
    for candidate in "$root/.local-deps/src/ps4-pkg-tools" "$common/.local-deps/src/ps4-pkg-tools"; do
        [[ -f $candidate/CMakeLists.txt ]] && { src=$candidate; break; }
    done
fi
[[ -f $src/CMakeLists.txt && -f $src/externals/cryptopp/cryptlib.h ]] \
    || { echo "No ps4-pkg-tools source (set BB_PKG_TOOL_SRC): ${src:-.local-deps/src/ps4-pkg-tools}" >&2; exit 1; }
# The patched copy: src/ copied, everything else (the big vendored externals) linked.
copy=$root/out/pkg-tool-src
rm -rf "$copy"
mkdir -p "$copy"
for entry in "$src"/*; do
    [[ ${entry##*/} == src ]] || ln -s "$entry" "$copy/${entry##*/}"
done
cp -a "$src/src" "$copy/src"
for patch in "$root"/tools/android/pkg-tool-patches/*.patch; do
    patch -d "$copy" -p1 -s < "$patch" || { echo "Patch failed: $patch" >&2; exit 1; }
done
src=$copy
sysroot=${BB_ARM64_SYSROOT:-$root/.local-deps/android/arm64-sysroot}
toolchain=$root/tools/android/aarch64-toolchain.cmake
# Android has no /lib: the interpreter is the fexdroid rootfs's, by absolute path (as bb-probe).
rootfs=${BB_THOR_ROOTFS:-/data/data/com.ducvd89.bloodborne/files/rootfs}
interpreter=$rootfs/usr/lib/aarch64-linux-gnu/ld-linux-aarch64.so.1

# The tool prints "Progress: N / M files" with plain "\n": linked into a pipe, stdout would be
# block-buffered and the app would see nothing until the end. This object (linked into the
# executable, run before main) makes stdout line-buffered.
shim() {   # <build dir> <compiler command...> -> prints the object path
    local dir=$1; shift
    mkdir -p "$dir"
    cat > "$dir/linebuf.c" <<'C'
#include <stdio.h>
__attribute__((constructor)) static void bb_line_buffered_stdout(void) { setvbuf(stdout, NULL, _IOLBF, 0); }
C
    "$@" -O2 -c "$dir/linebuf.c" -o "$dir/linebuf.o"
    echo "$dir/linebuf.o"
}

build_arm64() {
    [[ -f $sysroot/usr/lib/aarch64-linux-gnu/libc.so.6 ]] || { echo "No sysroot: $sysroot" >&2; exit 1; }
    local out=$root/out/arm64/pkg-tool build=$root/out/arm64/pkg-tool-build
    mkdir -p "$build"
    local cc=(clang --target=aarch64-linux-gnu --sysroot="$sysroot")
    local object; object=$(shim "$build" "${cc[@]}")
    # Static: the vendored cryptopp, the sysroot's zlib (libz.a), libstdc++ and libgcc. Only libc, libm
    # (and the dynamic loader) stay dynamic, which the rootfs has.
    cmake -S "$src" -B "$build" -G Ninja -DCMAKE_TOOLCHAIN_FILE="$toolchain" -DCMAKE_BUILD_TYPE=Release \
        -DBUILD_PKG_QT_GUI=OFF -DCRYPTOPP_BUILD_TESTING=OFF \
        -DZLIB_LIBRARY="$sysroot/usr/lib/aarch64-linux-gnu/libz.a" -DZLIB_INCLUDE_DIR="$sysroot/usr/include" \
        "-DCMAKE_EXE_LINKER_FLAGS=-fuse-ld=lld -no-pie -static-libstdc++ -static-libgcc -Wl,--dynamic-linker=$interpreter $object" \
        >"$build/cmake.log" 2>&1 || { tail -30 "$build/cmake.log" >&2; exit 1; }
    ninja -C "$build" ps4-pkg-tool >"$build/build.log" 2>&1 || { tail -40 "$build/build.log" >&2; exit 1; }
    mkdir -p "$out"
    cp "$build/ps4-pkg-tool" "$out/ps4-pkg-tool"
    llvm-strip --strip-all "$out/ps4-pkg-tool"
    if [[ -d $root/out/arm64/bundle/bin ]]; then
        cp "$out/ps4-pkg-tool" "$root/out/arm64/bundle/bin/ps4-pkg-tool"
    fi
    echo "Built $out/ps4-pkg-tool (aarch64)"
}

build_host() {
    local out=$root/out/host/pkg-tool build=$root/out/host/pkg-tool-build
    mkdir -p "$build"
    local object; object=$(shim "$build" cc)
    cmake -S "$src" -B "$build" -G Ninja -DCMAKE_BUILD_TYPE=Release -DBUILD_PKG_QT_GUI=OFF \
        -DCRYPTOPP_BUILD_TESTING=OFF "-DCMAKE_EXE_LINKER_FLAGS=$object" \
        >"$build/cmake.log" 2>&1 || { tail -30 "$build/cmake.log" >&2; exit 1; }
    ninja -C "$build" ps4-pkg-tool >"$build/build.log" 2>&1 || { tail -40 "$build/build.log" >&2; exit 1; }
    mkdir -p "$out"
    cp "$build/ps4-pkg-tool" "$out/ps4-pkg-tool"
    echo "Built $out/ps4-pkg-tool (host)"
}

case $which in
    arm64) build_arm64 ;;
    host) build_host ;;
    all) build_arm64; build_host ;;
    *) echo 'usage: build_pkg_tool.sh [arm64|host|all]' >&2; exit 2 ;;
esac
