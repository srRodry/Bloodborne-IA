# SM8475 performance experiment — dev10

Target: Motorola Edge 30 Ultra (Snapdragon 8+ Gen 1, Adreno 730, 12 GB).
Only development branch \`feature/edge30-ultra-sm8475\` is changed.

## Profiles available in Graphics settings

| Profile | Preupload budget per frame | Scan regions/frame | Vulkan recorders | Async pipeline workers | Texture idle |
|---|---:|---:|---:|---:|---:|
| Sustained (default) | 8 MiB | 96 | 2 | 2 | 30 s |
| Throughput | 16 MiB | 256 | 3 | 3 | 20 s |
| Legacy dev9 | 16 MiB | 256 | 2 | 2 | 20 s |

Mode 1 preupload refreshes only already resident memory. Reducing its per-frame
scan overhead is a CPU-side experiment; streaming may suffer if data is deferred
too long. Zero budget now disables the scan. No changes to PS4 file validation,
FEX TSO correctness, renderer sync, or forced GPU clocks. FEX Balanced, auto
GPU power, 240p internal rendering, FSR 3.1, lowest LOD and 30 FPS remain the
starting configuration.

The value \`BB_VRAM_IDLE_SECONDS\` remains 20, but normal in-place demotion is
inactive with \`BB_GUEST_IN_PLACE=0\`. Use env vars or \`thor-local.sh\` to
override individual limits, for diagnostics.

An existing dev9 installation with no \`performance_profile\` setting starts
with Sustained. Changing the profile never requires clearing saves or game
files. Select Legacy to restore the dev9 execution policy.

## A/B test on the phone

Repeat the same area, camera route, driver and ambient conditions on Legacy,
Sustained and Throughput. Compare FPS *and* 1% lows/frame times, crashes,
texture streaming and heat after 10–15 minutes. CI can verify the launcher,
scripts and APK build, not mobile sustained-FPS improvements.

\`python3 -m unittest discover -s tests -p 'test_sm8475_performance.py'\`
checks the actual launcher using a mock rootfs, including fallback and strict
renderer mode.
