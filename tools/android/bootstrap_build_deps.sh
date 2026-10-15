#!/usr/bin/env bash
# Recreate source/build dependencies needed by the Android ARM64 port.
# Does not download or require Bloodborne game files.
set -euo pipefail
cd -- "$(dirname -- "$0")/../.."

deps="$PWD/.local-deps/android"
prefix="$deps/arm64-prefix"
src="$deps/src"
mkdir -p "$prefix" "$src" "$PWD/.local-deps/src"

echo '[1/5] Repository submodules'
git submodule update --init --recursive

echo '[2/5] Pinned upstream Android rootfs/bridge source'
python3 tools/android/bootstrap_release_rootfs.py

echo '[3/5] Debian trixie ARM64 sysroot'
python3 tools/android/fetch_arm64_sysroot.py

echo '[4/6] Modern Vulkan headers'
VULKAN_HEADERS_COMMIT=8864cdc896bbc2a9b6eb36b3218fc9ef57908d77
vulkan_headers="$src/vulkan-headers"
if [[ ! -d $vulkan_headers/.git ]]; then
    git init -q "$vulkan_headers"
    git -C "$vulkan_headers" remote add origin https://github.com/KhronosGroup/Vulkan-Headers.git
fi
git -C "$vulkan_headers" fetch -q --depth 1 origin "$VULKAN_HEADERS_COMMIT"
git -C "$vulkan_headers" checkout -q --force FETCH_HEAD
git -C "$vulkan_headers" clean -qfdx
# Vulkan core headers include sibling vk_video/ headers. Install the complete include tree,
# not only include/vulkan, so the pinned header set remains internally consistent.
rm -rf "$prefix/include/vulkan" "$prefix/include/vk_video"
mkdir -p "$prefix/include"
cp -a "$vulkan_headers/include/." "$prefix/include/"
grep -q 'VK_VALVE_shader_mixed_float_dot_product' "$prefix/include/vulkan/vulkan_core.h" ||
    { echo 'Pinned Vulkan headers lack VK_VALVE_shader_mixed_float_dot_product' >&2; exit 1; }
grep -q 'VK_EXT_shader_float8' "$prefix/include/vulkan/vulkan_core.h" ||
    { echo 'Pinned Vulkan headers lack VK_EXT_shader_float8' >&2; exit 1; }

echo '[5/6] ARM64 miniz'
MINIZ_COMMIT=77d0dce8627735138c51770d1799a1ef48f2117d
miniz="$src/miniz"
if [[ ! -d $miniz/.git ]]; then
    git init -q "$miniz"
    git -C "$miniz" remote add origin https://github.com/richgel999/miniz.git
fi
git -C "$miniz" fetch -q --depth 1 origin "$MINIZ_COMMIT"
git -C "$miniz" checkout -q --force FETCH_HEAD
git -C "$miniz" clean -qfdx
rm -rf "$deps/miniz-build"
cmake -S "$miniz" -B "$deps/miniz-build" -G Ninja \
    -DCMAKE_TOOLCHAIN_FILE="$PWD/tools/android/aarch64-toolchain.cmake" \
    -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX="$prefix" \
    -DCMAKE_POLICY_VERSION_MINIMUM=3.5 \
    -DBUILD_EXAMPLES=OFF -DBUILD_TESTS=OFF -DBUILD_SHARED_LIBS=OFF -DINSTALL_PROJECT=ON \
    > "$deps/miniz-build.log" 2>&1 || { tail -40 "$deps/miniz-build.log" >&2; exit 1; }
ninja -C "$deps/miniz-build" install >> "$deps/miniz-build.log" 2>&1 ||
    { tail -40 "$deps/miniz-build.log" >&2; exit 1; }
[[ -f $prefix/lib/libminiz.a && -f $prefix/lib/cmake/miniz/minizConfig.cmake ]] ||
    { echo 'miniz install is incomplete' >&2; exit 1; }

echo '[6/6] Pinned ps4-pkg-tools source'
PKG_COMMIT=45baedaa29b8da42b3c6fe912800d2fa4119834b
pkg="$PWD/.local-deps/src/ps4-pkg-tools"
if [[ ! -d $pkg/.git ]]; then
    git init -q "$pkg"
    git -C "$pkg" remote add origin https://github.com/xXJSONDeruloXx/ps4-pkg-tools.git
fi
git -C "$pkg" fetch -q --depth 1 origin "$PKG_COMMIT"
git -C "$pkg" checkout -q --force FETCH_HEAD
git -C "$pkg" clean -qfdx
git -C "$pkg" submodule sync --recursive
git -C "$pkg" submodule update --init --recursive --depth 1

cat <<EOF
Android build dependencies are ready.

Rootfs:       .local-deps/android/migration/x/rootfs
ARM64 sysroot:.local-deps/android/arm64-sysroot
header prefix:.local-deps/android/arm64-prefix (Vulkan-Headers 1.4.350.1 + miniz)
pkg source:   .local-deps/src/ps4-pkg-tools
device libs:  .local-deps/android/device-libs.txt
bridge APK:   .local-deps/android/upstream/Bloodborne-0.8.4.apk
EOF
