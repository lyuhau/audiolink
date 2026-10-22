"""build_pyz.py: the same sources give the same .pyz, byte for byte; and the repo it names."""
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

    def test_names_its_repo(self):
        """A fork's build downloads the fork's release images (card.repo())."""
        import zipfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as d, mock.patch.dict("os.environ", {"GITHUB_REPOSITORY": "someone/fork"}):
            out = build_pyz.build(Path(d) / "audiolink.pyz")
            with zipfile.ZipFile(str(out)) as z:
                self.assertIn('REPO = "someone/fork"\n', z.read("audiolink/_build.py").decode())


class Repo(unittest.TestCase):
    def origin(self, url, env=""):
        from unittest import mock
        from audiolink import card
        with tempfile.TemporaryDirectory() as d, mock.patch.dict("os.environ", {"GITHUB_REPOSITORY": env}):
            subprocess.run(["git", "init", "-q", d], check=True)
            if url:
                subprocess.run(["git", "-C", d, "remote", "add", "origin", url], check=True)
            return card.checkout_repo(d)

    def test_from_origin(self):
        for url in ("https://github.com/someone/fork.git", "https://github.com/someone/fork",
                    "git@github.com:someone/fork.git", "ssh://git@github.com/someone/fork.git"):
            self.assertEqual(self.origin(url), "someone/fork", url)
        for url in ("", "https://gitlab.com/someone/fork.git", "/some/path"):
            self.assertIsNone(self.origin(url), url)
        self.assertEqual(self.origin("git@github.com:someone/fork.git", env="ci/repo"), "ci/repo")

    def test_from_source(self):
        from unittest import mock
        from audiolink import card
        with mock.patch.object(card, "checkout_repo", lambda root: None):
            self.assertEqual(card.repo(), card.UPSTREAM)        # not a GitHub checkout
        with mock.patch.object(card, "checkout_repo", lambda root: "someone/fork"):
            self.assertIn("/someone/fork/releases/", card.release_url("x.zip"))


if __name__ == "__main__":
    unittest.main()
