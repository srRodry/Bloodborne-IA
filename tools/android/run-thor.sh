#!/bin/sh
# Runs inside fexdroid's Android app sandbox, with its patched ARM Linux runtime.
set -eu
files=${FXD_FILES:-/data/data/com.ducvd89.bloodborne/files}
host="$files/rootfs"
base="$files/bbport"
export FXD_FILES="$files" FXD_ROOT="$host"
export PATH="$host/usr/bin:$host/bin:/system/bin"
export HOME="$files/home/bbport"
export TMPDIR="$host/tmp" XDG_RUNTIME_DIR="$host/tmp"
export FEX_ROOTFS="$base/rootfs"
export FEX_APP_CONFIG_LOCATION="$HOME/.fex-emu/"
export FEX_APP_DATA_LOCATION="$HOME/.fex-emu/"
export FEX_APP_CACHE_LOCATION="$HOME/.cache/fex-emu/"
export GLIBC_TUNABLES='glibc.cpu.hwcaps=-AVX2,-AVX,-AVX_Fast_Unaligned_Load,-AVX2_Usable,-AVX_Usable'
export BB_CONFIG="$base/bbport.ini"
[ -f "$BB_CONFIG" ] || cp "$base/bbport.default.ini" "$BB_CONFIG"
setting() { sed -n "s/^$1=//p" "$BB_CONFIG" 2>/dev/null | tail -n 1; }
# The GPU driver. One the player installed from a zip (the app's GPU driver menu, DriverManager.java)
# turns the automatic choice off: drivers/<gpu_driver>/icd.json names its library there, the zip's
# other libraries beside it. Without one (gpu_driver empty or not installed), the bundled Turnip by
# GPU: Mesa main for Adreno 6xx/7xx (the Thor's 740, chip id 0x43...), the turnip/gen8 build on
# Adreno 8xx (0x44..., e.g. the 8 Elite's 830). BB_TURNIP=main|gen8 overrides that choice.
gpu_driver=$(setting gpu_driver)
case $gpu_driver in .*|*/*) gpu_driver= ;; esac
kgsl_chip=$("$base/arm64/bin/kgsl-chip-id" 2>/dev/null || true)
page_size=$(getconf PAGESIZE 2>/dev/null || true)
cpu_features=$(sed -n 's/^Features[[:space:]]*:[[:space:]]*//p' /proc/cpuinfo 2>/dev/null | head -n 1)
case " $cpu_features " in *" sve2 "*) sve2=yes ;; *) sve2=no ;; esac
echo "Device probe: KGSL=${kgsl_chip:-unknown}, page=${page_size:-unknown}, SVE2=$sve2"
if [ -n "$page_size" ] && [ "$page_size" != 4096 ]; then
    echo "Unsupported Android page size: $page_size (this runtime currently requires 4096)" >&2
    exit 2
fi
if [ -n "$gpu_driver" ] && [ -f "$files/drivers/$gpu_driver/icd.json" ]; then
    icd="$files/drivers/$gpu_driver/icd.json"
    export LD_LIBRARY_PATH="${LD_LIBRARY_PATH:+$LD_LIBRARY_PATH:}$files/drivers/$gpu_driver"
    echo "GPU driver: $gpu_driver (installed from a zip, automatic choice off)"
else
    [ -n "$gpu_driver" ] && echo "GPU driver $gpu_driver is not installed: automatic choice"
    turnip=${BB_TURNIP:-}
    if [ -z "$turnip" ]; then
        case $kgsl_chip in
            0x44*) turnip=gen8 ;;
            *) turnip=main ;;
        esac
    fi
    icd="$host/usr/share/vulkan/icd.d/freedreno_icd.aarch64.json"
    [ "$turnip" = gen8 ] && [ -f "$host/usr/share/vulkan/icd.d/freedreno_gen8_icd.aarch64.json" ] &&
        icd="$host/usr/share/vulkan/icd.d/freedreno_gen8_icd.aarch64.json"
    echo "Turnip: ${icd##*/}"
fi
export VK_DRIVER_FILES="$icd"
export VK_ICD_FILENAMES="$VK_DRIVER_FILES"
# Optional KGSL constraint for diagnosing governor under-clocking. "max" is intentionally not
# the default on phones: higher instantaneous clocks can turn into worse sustained performance
# once the thermal governor reacts. Custom drivers that lack the local Turnip patch ignore it.
gpu_power=$(setting gpu_power)
case $gpu_power in
    max) export TU_KGSL_PWR_CONSTRAINT=max; echo "GPU power constraint: max" ;;
    *) unset TU_KGSL_PWR_CONSTRAINT 2>/dev/null || true; gpu_power=auto; echo "GPU power constraint: automatic" ;;
esac
if [ "${BB_ANDROID_INPUT:-0}" = 1 ]; then
    export BB_PAD_FILE="$base/android-pad.state" BB_PAD_QUIET=1
    # Text the game asks for (the character name): the app's touch keyboard.
    export BB_IME_FILE="$base/ime.request"
    # Sound: SDL's disk driver mixes into the FIFO the app plays (AudioBridge.java). Only when the
    # app made it: a FIFO nobody reads would stall the game's audio. Its reads pace the output.
    if [ -p "$base/audio.fifo" ]; then
        export SDL_AUDIO_DRIVER=disk SDL_AUDIO_DISK_OUTPUT_FILE="$base/audio.fifo"
        export SDL_AUDIO_DISK_TIMESCALE=0 SDL_AUDIO_DEVICE_SAMPLE_FRAMES=512
    fi
fi
# Graphics settings (the app's settings screen writes bbport.ini). The output is the Thor's
# 1280x720 display; the preset sets the scene size, FSR 3.1 upscales it to the output.
upscaler=$(setting upscaler); preset=$(setting preset)
# FSR 4 and 4.1.1 (INT8 models) are not offered: on the Thor's Adreno 740 FSR 4 took ~3 min to
# compile in Turnip, then ran at 2.4 FPS (~470 ms of GPU time a frame) with a black scene, and with
# the Lossless Scaling layer it crashed in Turnip. Any value but off means FSR 3.1.
case $upscaler in off) ;; *) upscaler=fsr3 ;; esac
echo "Upscaler setting: $upscaler"
case $preset in 0|1|2|3|4) ;; *) preset=3 ;; esac
render=1280x720
if [ "$upscaler" != off ]; then
    case $preset in 1) render=854x480 ;; 2) render=752x424 ;; 3) render=640x360 ;; 4) render=426x240 ;; esac
fi
export BB_FULLSCREEN=1 BB_UPSCALER=$upscaler BB_UPSCALE_PRESET=$preset
# FSR 3.1 frame generation (frame_generation=1): a frame between each two, with FSR 3.1 only.
[ "$(setting frame_generation)" = 1 ] && [ "$upscaler" = fsr3 ] && export BB_FRAME_GEN=1
# The GPU's check of indirect dispatch counts (upstream 0.5, vk_indirect_guard.h) is off: it was
# suspected of 0.5.0's hangs on the Thor (those came from the Lossless Scaling layer's later
# changes, undone in 0.5.1) and is not tested here on its own. BB_INDIRECT_GUARD=1 in
# thor-local.sh turns it on.
export BB_INDIRECT_GUARD=${BB_INDIRECT_GUARD:-0}
# The two-stage draw pipeline (bb:DrawRec) turns itself on only with 8 or more CPUs the game may
# use, so thor_cpus=3-7 turned it off. On, it frees the GPU command thread: 31 -> 34 FPS on the
# Thor with all cores. BB_DRAW_PIPE=0 in thor-local.sh turns it off.
export BB_DRAW_PIPE=${BB_DRAW_PIPE:-1}
# SM8475 renderer synchronization A/B. "normal" preserves the fast path. "buffer-safe"
# disables deferred copies of small read-only buffers, forcing their guest bytes to be copied
# synchronously before the draw can use them. This directly targets the BufferCache path that
# documents vertex explosions when a source changes before the deferred copy runs.
# "strict" adds a single Vulkan recorder, synchronous submit, and disables the draw pipeline.
renderer_sync=$(setting renderer_sync)
case $renderer_sync in
    buffer-safe)
        toggle_file="$base/renderer-sync.toggles"
        printf '16384 0\n' > "$toggle_file"
        export BB_TOGGLE_FILE="$toggle_file"
        echo 'Renderer sync: buffer-safe (synchronous small guest buffer copies)'
        ;;
    resource-safe)
        # PreparedResources(4096) + DrawPreparation(8192) + EarlyDrawInputs(1<<35).
        # This keeps the fast draw pipeline but forces vertex/index/resource descriptors to be
        # resolved from the draw packet/current registers on the recording path.
        toggle_file="$base/renderer-sync.toggles"
        printf '34359750656 0\n' > "$toggle_file"
        export BB_TOGGLE_FILE="$toggle_file"
        echo 'Renderer sync: resource-safe (no prepared resources/draws or early draw inputs)'
        ;;
    strict)
        # Resource-safe plus DeferredStreamCopies(16384), then serialize Vulkan recording/submission.
        toggle_file="$base/renderer-sync.toggles"
        printf '34359767040 0\n' > "$toggle_file"
        export BB_TOGGLE_FILE="$toggle_file"
        export BB_DRAW_PIPE=0 BB_VK_RECORD_THREADS=1 BB_ASYNC_SUBMIT=0
        echo 'Renderer sync: strict (resource-safe + buffer-safe + one recorder + sync submit + draw pipe off)'
        ;;
    *)
        renderer_sync=normal
        rm -f "$base/renderer-sync.toggles"
        unset BB_TOGGLE_FILE 2>/dev/null || true
        echo 'Renderer sync: normal'
        ;;
esac
# Lossless Scaling frame generation (lsfg_multiplier 2-4): the lsfg-vk 1.0 layer with our Turnip
# patches (bbport/arm64/lsfg, build_lsfg.sh) and Lossless.dll from the player's own Lossless
# Scaling (Steam), next to the game folder or in bbport/. It replaces FSR 3.1 frame generation.
# Its messages (lsfg-vk: ...) go to the game log.
lsfg=$(setting lsfg_multiplier)
case $lsfg in 2|3|4) ;; *) lsfg= ;; esac
if [ -n "$lsfg" ] && [ -f "$base/arm64/lsfg/VkLayer_LS_frame_generation.json" ]; then
    lsfg_dll=
    for dll in "${BB_GAME_DIR:+${BB_GAME_DIR%/*}/Lossless.dll}" "$base/Lossless.dll"; do
        [ -n "$dll" ] && [ -f "$dll" ] && { lsfg_dll=$dll; break; }
    done
    if [ -n "$lsfg_dll" ]; then
        flow=$(setting lsfg_flow_scale)
        case $flow in 0.25|0.5|0.75|1|1.0) ;; *) flow=1.0 ;; esac
        performance=$([ "$(setting lsfg_performance)" = 1 ] && echo true || echo false)
        unset BB_FRAME_GEN
        # Config file mode: lsfg-vk watches it, so the app changes the multiplier (1: off), flow
        # scale and performance mode while the game runs (SettingsDialog.showLossless). The file
        # exists only while the layer is loaded; BB_LSFG: vk_swapchain.cpp restarts without it if
        # it fails. An implicit layer, so the layer's own framegen instance skips it (DISABLE_LSFG).
        # Present mode: FIFO paces the generated frames (LSFG_PRESENT=mailbox|immediate to try).
        export BB_LSFG=1 LSFG_CONFIG="$base/lsfg-vk.toml"
        export VK_ADD_IMPLICIT_LAYER_PATH="$base/arm64/lsfg"
        cat > "$LSFG_CONFIG.tmp" <<EOF
version = 1

[global]
dll = "$lsfg_dll"

[[game]]
exe = "bb-probe"
multiplier = $lsfg
flow_scale = $flow
performance_mode = $performance
experimental_present_mode = "${LSFG_PRESENT:-fifo}"
EOF
        mv -f "$LSFG_CONFIG.tmp" "$LSFG_CONFIG"
        echo "Lossless Scaling frame generation: x$lsfg, flow scale $flow, $lsfg_dll"
    else
        lsfg=; echo 'Lossless Scaling frame generation: no Lossless.dll next to the game folder'
    fi
else
    lsfg=
fi
[ -n "$lsfg" ] || rm -f "$base/lsfg-vk.toml"
# FSR 4 model assets (fetch_fsr4_assets.sh), in the native bundle when fetched.
[ -d "$base/arm64/fsr4_shaders" ] && export BB_FSR4_DIR="$base/arm64/fsr4_shaders"
export BB_WINDOW_SIZE=1280x720
export BB_RENDER_RES=$render BB_OUTPUT_RES=1280x720 BB_LIVE_RES=0
export BB_DMEM_MB=5056 BB_PREUPLOAD=0 BB_GUEST_IN_PLACE=0 BB_UFFD=0
export BB_COPY_GPU_BUFFERS=1 BB_GPU_WRITE_TWINS=1 BB_GPU_WRITE_TWINS_MAX=65536
# Keep the two renderer memory lifetimes separate. They control different caches:
# BB_GC_IDLE_SECONDS is texture-cache eviction; BB_VRAM_IDLE_SECONDS is BufferCache residency.
# The first SM8475 dev build accidentally stopped exporting BB_VRAM_IDLE_SECONDS, making buffer
# copies fall back to 60 s instead of the upstream Android baseline of 20 s. Restore the proven
# upstream lifetime/budget semantics before attempting any further memory optimisation.
BB_GC_BUDGET_MB=${BB_GC_BUDGET_MB:-4096}
BB_GC_IDLE_SECONDS=${BB_GC_IDLE_SECONDS:-20}
BB_VRAM_IDLE_SECONDS=${BB_VRAM_IDLE_SECONDS:-20}
export BB_GC_BUDGET_MB BB_GC_IDLE_SECONDS BB_VRAM_IDLE_SECONDS
mem_kb=$(awk '/^MemTotal:/{print $2; exit}' /proc/meminfo 2>/dev/null || true)
mem_avail_kb=$(awk '/^MemAvailable:/{print $2; exit}' /proc/meminfo 2>/dev/null || true)
echo "Memory profile: MemTotal=${mem_kb:-unknown} KiB, MemAvailable=${mem_avail_kb:-unknown} KiB, texture budget=${BB_GC_BUDGET_MB} MiB, texture idle=${BB_GC_IDLE_SECONDS}s, buffer idle=${BB_VRAM_IDLE_SECONDS}s"
# FPS limit (fps_limit): 30 is the game's own timing; above it the delta-time patch
# (fps=uncap.bin) with presents at once (vblank 480 Hz, as run.sh) and BB_FPS_LIMIT (0: none).
fps_limit=$(setting fps_limit)
case $fps_limit in 40|45|60|0) export BB_VBLANK_HZ=480 BB_FPS_LIMIT=$fps_limit ;;
    *) fps_limit=30; export BB_VBLANK_HZ=60 ;; esac
# Frame generation adds frames on screen only: at the game's own 30 FPS timing a slower frame
# still slows the game down. With it, 30 means the delta-time patch at 30 real frames a second.
if { [ "${BB_FRAME_GEN:-0}" = 1 ] || [ -n "$lsfg" ]; } && [ "$fps_limit" = 30 ]; then
    fps_limit=30-delta; export BB_VBLANK_HZ=480 BB_FPS_LIMIT=30
fi
# CPU cores (thor_cpus, a taskset list such as 3-7): empty for all of them.
[ -z "${BB_THOR_CPUS:-}" ] && BB_THOR_CPUS=$(setting thor_cpus)
# Diagnostics stay off in releases: BB_FRAME_STATS (per-frame timers and stall logs; any value
# turns them on) and BB_PRESENT_DUMP_TRIGGER go in thor-local.sh when needed.
export SDL_VIDEODRIVER=x11 DISPLAY=:0
export MESA_VK_WSI_DEBUG=sw MESA_VK_WSI_PRESENT_MODE=relaxed
export FEXDROID_PRESENT="$host/tmp/fxpresent.sock"
export SDL_JOYSTICK_HIDAPI=0
# Find the built-in controller again after a reboot (event numbers can change).
for thor_node in /sys/class/input/event*; do
    [ -r "$thor_node/device/id/vendor" ] && [ -r "$thor_node/device/id/product" ] || continue
    IFS= read -r thor_vendor < "$thor_node/device/id/vendor"
    IFS= read -r thor_product < "$thor_node/device/id/product"
    if [ "$thor_vendor:$thor_product" = '2020:0112' ]; then
        export SDL_JOYSTICK_DEVICE="/dev/input/${thor_node##*/}"
        break
    fi
done
export SDL_GAMECONTROLLERCONFIG_FILE="$base/thor-gamecontrollerdb.txt"
export BB_GAMEPAD='030018dc202000001201000000000000'
export PULSE_SERVER="unix:$host/tmp/pulse/native" PULSE_LATENCY_MSEC=60
mkdir -p "$HOME/.fex-emu" "$HOME/.cache/fex-emu" "$base/user" "$base/logs"
# FEX execution profile. "fast" preserves the old settings (TSO off); "balanced" restores
# x86 memory ordering while retaining reduced x87 precision; "stable" additionally enables
# vector/memcpy TSO and full x87 precision. Balanced is the default for device-focused builds.
fex_profile=${BB_FEX_PROFILE:-$(setting fex_profile)}
case $fex_profile in
    fast) fex_config="$base/fex-compatible.json" ;;
    stable) fex_config="$base/fex-stable.json" ;;
    balanced|'') fex_profile=balanced; fex_config="$base/fex-balanced.json" ;;
    *) echo "Unknown FEX profile '$fex_profile'; using balanced"; fex_profile=balanced; fex_config="$base/fex-balanced.json" ;;
esac
if [ ! -f "$fex_config" ]; then
    echo "FEX profile file missing: $fex_config; falling back to legacy fast profile"
    fex_profile=fast
    fex_config="$base/fex-compatible.json"
fi
cp "$fex_config" "$HOME/.fex-emu/Config.json"
echo "FEX profile: $fex_profile (${fex_config##*/})"
# Local overrides for this device (experiments, tuning): sourced last, not part of the bundle.
[ -f "$base/thor-local.sh" ] && . "$base/thor-local.sh"
cd "$base"
case ${1:-game} in
    gamepads)
        exec "$host/usr/bin/FEX" "$base/bin/bb-gpu-capabilities" --gamepads
        ;;
    controller)
        exec "$host/usr/bin/FEX" "$base/bin/controller-probe" 120
        ;;
    cpu)
        exec "$host/usr/bin/FEX" "$base/bin/bb-probe" "$base/data/cpu-fixture.bin" --cpu-only
        ;;
    vulkan)
        exec "$host/usr/bin/FEX" "$base/bin/bb-probe" --vulkan-only
        ;;
    renderer)
        exec "$host/usr/bin/FEX" "$base/bin/renderer-probe"
        ;;
    window)
        exec "$host/usr/bin/FEX" "$base/bin/bb-probe" "$base/data/cpu-fixture.bin" --timeout 60
        ;;
    game)
        # The game folder chosen in the app (shared storage), else the bundle's own. If a shadPS4-
        # style 1.09 update folder sits beside it (CUSA...-UPDATE or -patch), layer every update
        # file over the base instead of relying on a manual merge that can leave stale files.
        selected_game_dir=${BB_GAME_DIR:-$base/game}
        game_dir=$selected_game_dir
        [ -f "$selected_game_dir/eboot.bin" ] || { echo "Game missing: $selected_game_dir"; exit 2; }
        if [ -x "$host/usr/bin/python3.13" ]; then
            update_game=$(PYTHONHOME="$host/usr" "$host/usr/bin/python3.13" \
                "$base/scripts/mods_android.py" update-overlay --game "$selected_game_dir" \
                --out "$base/update-game" | tail -n 1) || update_game=
            if [ -n "$update_game" ] && [ -f "$update_game/eboot.bin" ]; then
                game_dir=$update_game
            else
                echo 'Game update overlay: unusable result; using selected folder'
                game_dir=$selected_game_dir
                rm -rf "$base/update-game"
            fi
            export BB_EFFECTIVE_GAME_DIR="$game_dir"
            PYTHONHOME="$host/usr" "$host/usr/bin/python3.13" \
                "$base/scripts/prepare_game.py" "$game_dir" "$base/data"
        elif [ ! -f "$base/data/boot-linked.bin" ]; then
            echo 'Python runtime missing; cannot prepare the selected game files' >&2
            exit 2
        fi
        # Loose-file mods (mods_enabled, default on): a symlink overlay of the game folder, built
        # after prepare_game.py (it needs the original eboot). mods_android.py prints the folder to
        # run (the overlay, or the game itself when no valid enabled mod); it never fails the launch.
        mods_dir=$(setting mods_dir)
        [ -n "$mods_dir" ] || mods_dir=/storage/emulated/0/Bloodborne/mods
        export BB_MODS_DIR="$mods_dir"
        [ -n "${BB_MOD_TRACE:-}" ] && export BB_MOD_TRACE
        app0_dir=$game_dir
        if [ "$(setting mods_enabled)" != 0 ] && [ -x "$host/usr/bin/python3.13" ]; then
            mods_game=$(PYTHONHOME="$host/usr" "$host/usr/bin/python3.13" "$base/scripts/mods_android.py" apply \
                --game "$game_dir" --out "$base/mod-game" --mods-dir "$mods_dir" \
                --config "$base/mods.json" | tail -n 1) || mods_game=
            if [ -n "$mods_game" ] && [ -f "$mods_game/eboot.bin" ]; then
                app0_dir=$mods_game
            else
                echo 'Mods: could not be set up, starting without them'
            fi
        else
            echo 'Mods: off'
        fi
        [ "$app0_dir" = "$game_dir" ] && rm -rf "$base/mod-game"
        # The native ARM64 build (runtime and GPU native, the game's code in FEXCore) when it is
        # installed; BB_THOR_X86=1: the whole x86-64 loader under FEX instead.
        if [ -x "$base/arm64/bin/bb-probe" ] && [ "${BB_THOR_X86:-0}" != 1 ]; then
            unset GLIBC_TUNABLES
            set -- "$base/arm64/bin/bb-probe"; native=1
            # BB_THOR_CPUS: the CPUs the game may run on (taskset -c list), e.g. 3-7 big cores only.
            [ -n "${BB_THOR_CPUS:-}" ] && set -- taskset -c "$BB_THOR_CPUS" "$@"
        else
            set -- "$host/usr/bin/FEX" "$base/bin/bb-probe"; native=0
        fi
        # Pre-compiled patches for the settings (build_thor_patch_parts.py; the native loader
        # takes several), else the bundle's single file.
        parts="$base/arm64/patch-parts"
        if [ "$native" = 1 ] && [ -d "$parts" ]; then
            set -- "$@" "$base/data/boot-linked.bin" --patches "$parts/res-$render.bin"
            for key in effect_chromatic_aberration effect_dof effect_motion_blur effect_ssao \
                       effect_game_aa effect_dynamic_shadows effect_ssr skip_intro debug_camera debug_menu model_lod; do
                value=$(setting "$key")
                [ -f "$parts/$key=$value.bin" ] && set -- "$@" --patches "$parts/$key=$value.bin"
            done
            [ "$fps_limit" != 30 ] && set -- "$@" --patches "$parts/fps=uncap.bin"
            # No PSN here: the title screen goes straight to the main menu offline.
            [ "$(setting skip_network_choice)" != 0 ] && [ -f "$parts/skip_network_choice=1.bin" ] &&
                set -- "$@" --patches "$parts/skip_network_choice=1.bin"
            # Cheats (the app's Cheats menu): their hooks are always in, switched live by the flags
            # BB_CHEATS_FILE holds (the app rewrites it; here its start, from bbport.ini).
            if [ -f "$parts/cheats.bin" ]; then
                set -- "$@" --patches "$parts/cheats.bin"
                export BB_CHEATS_FILE="$base/cheats.state"
                printf '%s%s\n' "$([ "$(setting cheat_health)" = 1 ] && echo 1 || echo 0)" \
                    "$([ "$(setting cheat_items)" = 1 ] && echo 1 || echo 0)" > "$BB_CHEATS_FILE"
            fi
        else
            set -- "$@" "$base/data/boot-linked.bin" --patches "$base/data/patches.bin"
        fi
        exec "$@" \
            --content-profile "$base/data/content.bin" \
            --app0 "$app0_dir" --user "$base/user" --timeout "${BB_TIMEOUT:-0}"
        ;;
    *) echo 'usage: run-thor.sh cpu|vulkan|renderer|window|gamepads|controller|game' >&2; exit 2 ;;
esac
