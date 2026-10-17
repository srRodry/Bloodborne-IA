"""SM8475 launcher integration tests using mocked ARM Linux binaries."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]


class SM8475PerformanceTests(unittest.TestCase):
    def launch(self, profile="", extra=None, sync="normal"):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            bin_dir = home / "rootfs/usr/bin"
            bin_dir.mkdir(parents=True)
            base = home / "bbport"
            (base / "arm64/bin").mkdir(parents=True)
            for cmd in ("sed", "awk", "head", "getconf", "cp", "mkdir", "rm", "tail"):
                exe = shutil.which(cmd)
                self.assertIsNotNone(exe, cmd)
                (bin_dir / cmd).symlink_to(exe)
            chip = base / "arm64/bin/kgsl-chip-id"
            chip.write_text("#!/bin/sh\nprintf '0x43'\n")
            chip.chmod(0o755)
            fex = bin_dir / "FEX"
            fex.write_text("""#!/bin/sh
echo "PROFILE=$BB_PERFORMANCE_PROFILE"
echo "UPLOAD=$BB_PREUPLOAD_MB"
echo "SCAN=$BB_PREUPLOAD_SCAN_REGIONS"
echo "RECORD=$BB_VK_RECORD_THREADS"
echo "COMPILE=$BB_ASYNC_PIPELINE_THREADS"
echo "TEXTURE_IDLE=$BB_GC_IDLE_SECONDS"
""")
            fex.chmod(0o755)
            (base / "fex-balanced.json").write_text('{"Config":{}}\n')
            config = "upscaler=fsr3\npreset=4\nrenderer_sync=" + sync + "\n"
            config += "fex_profile=balanced\ngpu_power=auto\nfps_limit=30\n"
            config += "frame_generation=0\nlsfg_multiplier=0\nthor_cpus=\n"
            if profile:
                config += "performance_profile=" + profile + "\n"
            (base / "bbport.ini").write_text(config)
            env = {"FXD_FILES": str(home), "HOME": str(home),
                   "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C"}
            env.update(extra or {})
            result = subprocess.run(
                ["dash", str(REPO / "tools/android/run-thor.sh"), "cpu"],
                cwd=REPO, env=env, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return dict(line.split("=", 1) for line in result.stdout.splitlines()
                        if line.startswith(("PROFILE=", "UPLOAD=", "SCAN=",
                                            "RECORD=", "COMPILE=", "TEXTURE_IDLE=")))

    def test_default_and_unknown(self):
        expected = dict(PROFILE="sustained", UPLOAD="8", SCAN="96",
                        RECORD="2", COMPILE="2", TEXTURE_IDLE="30")
        self.assertEqual(self.launch(), expected)
        self.assertEqual(self.launch("invalid-profile"), expected)

    def test_throughput(self):
        self.assertEqual(self.launch("throughput"),
                         dict(PROFILE="throughput", UPLOAD="16", SCAN="256",
                              RECORD="3", COMPILE="3", TEXTURE_IDLE="20"))

    def test_legacy_dev9(self):
        self.assertEqual(self.launch("legacy"),
                         dict(PROFILE="legacy", UPLOAD="16", SCAN="256",
                              RECORD="2", COMPILE="2", TEXTURE_IDLE="20"))

    def test_overrides_and_strict_renderer(self):
        got = self.launch("throughput", {
            "BB_PREUPLOAD_MB": "4", "BB_PREUPLOAD_SCAN_REGIONS": "48",
            "BB_ASYNC_PIPELINE_THREADS": "1"}, sync="strict")
        self.assertEqual((got["UPLOAD"], got["SCAN"], got["RECORD"], got["COMPILE"]),
                         ("4", "48", "1", "1"))


if __name__ == "__main__":
    unittest.main()
