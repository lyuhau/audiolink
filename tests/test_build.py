"""build_pyz.py: the same sources give the same .pyz, byte for byte."""
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import build_pyz


class Pyz(unittest.TestCase):
    def test_reproducible(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            # A copy has fresh file times (shutil.copy keeps none): only the bytes match.
            copy = shutil.copytree(build_pyz.ROOT / "audiolink", d / "src" / "audiolink",
                                   copy_function=shutil.copy,
                                   ignore=shutil.ignore_patterns("__pycache__"))
            (Path(copy) / "__init__.py").chmod(0o600)
            a = build_pyz.build(d / "a.pyz")
            b = build_pyz.build(d / "b.pyz", pkg=Path(copy))
            self.assertEqual(a.read_bytes(), b.read_bytes())

    def test_runs(self):
        with tempfile.TemporaryDirectory() as d:
            out = build_pyz.build(Path(d) / "audiolink.pyz")
            v = subprocess.run([sys.executable, str(out), "--version"], capture_output=True, text=True)
            self.assertEqual(v.returncode, 0, v.stderr)
            import audiolink
            self.assertIn(audiolink.__version__, v.stdout + v.stderr)

    def test_build_id(self):
        from audiolink.services.update import pyz_build
        with tempfile.TemporaryDirectory() as d:
            out = build_pyz.build(Path(d) / "audiolink.pyz")
            bid = pyz_build(str(out))
            self.assertRegex(bid, r"^[0-9a-f]{12}$")
            self.assertEqual(bid, build_pyz.source_build_id())          # the app's, from the same sources
            v = subprocess.run([sys.executable, str(out), "--version"], capture_output=True, text=True)
            self.assertIn("(build %s)" % bid, v.stdout)
        self.assertIsNone(pyz_build(__file__))                          # not a pyz


if __name__ == "__main__":
    unittest.main()
