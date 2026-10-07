"""Saved state (paths.load_json/save_json) and terminal output (ui)."""
import contextlib
import io
import json
import os
import tempfile
import unittest

from audiolink import paths, ui


class Storage(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "state.json")

    def test_round_trip_private(self):
        paths.save_json(self.path, {"a": 1})
        self.assertEqual(paths.load_json(self.path), {"a": 1})
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)
        self.assertEqual([f for f in os.listdir(self.dir.name) if ".tmp-" in f], [])

    def test_missing_is_the_default(self):
        self.assertEqual(paths.load_json(self.path, {}), {})

    def test_damaged_file_is_moved_aside_not_overwritten(self):
        with open(self.path, "w") as f:
            f.write('{"seed": "abc"')                     # cut short
        self.assertEqual(paths.load_json(self.path, {}), {})
        with open(self.path + ".unreadable") as f:
            self.assertIn("seed", f.read())
        self.assertFalse(os.path.exists(self.path))
        paths.save_json(self.path, {"new": True})
        self.assertTrue(os.path.exists(self.path + ".unreadable"))
        self.assertEqual(json.load(open(self.path)), {"new": True})


class Output(unittest.TestCase):
    def tearDown(self):
        ui.VERBOSE = False

    def test_detail_only_with_verbose_and_before_any_step(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ui.detail("hidden %s", 1)
            ui.VERBOSE = True
            ui.detail("shown %s", 2)
        self.assertNotIn("hidden", out.getvalue())
        self.assertIn("shown 2", out.getvalue())

    def test_step_and_done_make_one_line(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ui.step("Checking X")
            ui.done("ok")
        self.assertEqual(out.getvalue(), "Checking X... ok\n")


if __name__ == "__main__":
    unittest.main()
