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


class State(unittest.TestCase):
    """save_state/load_state: the Pi's state, for a card that can lose power mid-write."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "networks.json")

    def test_round_trip_and_newest_wins(self):
        for i in range(5):
            paths.save_state(self.path, {"n": i})
            self.assertEqual(paths.load_state(self.path), {"n": i})
        self.assertEqual(sorted(os.listdir(self.dir.name)), ["networks.json.a", "networks.json.b"])
        self.assertEqual({os.path.getsize(os.path.join(self.dir.name, f)) for f in os.listdir(self.dir.name)},
                         {paths.SLOT_SIZE})

    def test_a_torn_write_leaves_the_previous_copy(self):
        paths.save_state(self.path, {"n": 1})
        paths.save_state(self.path, {"n": 2})              # in slot b
        with open(self.path + ".b", "r+b") as f:           # power lost mid-write of slot b
            f.seek(20)
            f.write(b"garbage")
        self.assertEqual(paths.load_state(self.path), {"n": 1})
        paths.save_state(self.path, {"n": 3})              # overwrites the damaged slot
        self.assertEqual(paths.load_state(self.path), {"n": 3})

    def test_reads_a_file_from_before_slots(self):
        paths.save_json(self.path, {"old": True})
        self.assertEqual(paths.load_state(self.path, {}), {"old": True})
        paths.save_state(self.path, {"new": True})
        self.assertEqual(paths.load_state(self.path, {}), {"new": True})

    def test_missing_is_the_default(self):
        self.assertEqual(paths.load_state(self.path, {}), {})


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
