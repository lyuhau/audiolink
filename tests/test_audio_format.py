"""Audio-format negotiation: host/audio_format.py against a simulated Pi, and the Pi's
refusal of formats it doesn't know (services/audio_format.py)."""
import base64
import contextlib
import hashlib
import io
import os
import shutil
import tempfile
import unittest
from unittest import mock

from audiolink.host import audio_format as af
from audiolink.services.audio_format import AudioFormatService
from audiolink.wire.fmt import BASELINE, AudioFormat

from .helpers import FakeClock, patch_time, wait

F192x8, F96x8, F96x2 = AudioFormat(192000, 8), AudioFormat(96000, 8), AudioFormat(96000, 2)


class FakeLink:
    def __init__(self):
        self.format = BASELINE
        self.stats = {"retx": 0, "crc_fail": 0}


class FakePi:
    """A session whose peer is a Pi: switches the 'gadget' (counted as rebinds), runs
    the formats in `works`, and moves data at half the format's raw rate."""

    def __init__(self, clock, works=(), current=BASELINE):
        self.clock, self.works = clock, set(works)
        self.link = FakeLink()
        self.peer_alive = True
        self.peer_info = {"identity": {"serial": "s1"}, "audio": {"switchable": True}}
        self.format, self.pending, self.rebinds = current, False, 0
        self.link.format = current
        self.on = {}                            # op -> hook called before answering

    def _present(self, fmt):
        self.rebinds += 1
        self.format = self.link.format = fmt

    def request(self, op, args=None, timeout=60):
        if not self.peer_alive:
            raise RuntimeError("No Pi plugged in")
        if op in self.on:
            self.on[op]()
        args = args or {}
        if op == "audio.status":
            return {"format": self.format.to_json(), "pending": self.pending}
        if op == "audio.switch":
            fmt = AudioFormat.from_json(args["format"])
            if fmt in self.works:
                self._present(fmt)
                self.pending = True
            else:
                self._present(BASELINE)
                self.pending = False
            return {}
        if op == "audio.revert":
            if self.format != BASELINE:
                self._present(BASELINE)
            self.pending = False
            return {}
        if op == "audio.confirm":
            self.pending = False
            return {}
        if op == "audio.train":
            up = base64.b64decode(args["up"])
            self.clock.advance(2 * len(up) / (self.format.bytes_per_sec / 2))
            down = os.urandom(args["down"])
            return {"up_sha": hashlib.sha256(up).hexdigest(), "down": base64.b64encode(down).decode(),
                    "down_sha": hashlib.sha256(down).hexdigest()}
        raise AssertionError(op)


class Negotiation(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        self.clock = FakeClock()
        p = patch_time(af, self.clock)
        p.start()
        self.addCleanup(p.stop)
        self.neg = af.AudioNegotiator()
        self.neg.store = af._Store(os.path.join(d, "audio_formats.json"))

    def pi(self, **kw):
        self.p = FakePi(self.clock, **kw)
        self.neg.attach(self.p)
        return self.p

    def mem(self):
        return self.neg.store.load("s1")

    def run_neg(self, sweep=False, target=None, chosen=False):
        self.neg._run(sweep, target, chosen)
        return self.neg.outcome

    def test_parse_format_only_known(self):
        for bad in ("0x0", "-1x2", "1x64", "48000x200", "96000", "fast", "x"):
            with self.assertRaisesRegex(ValueError, "pick baseline or one of", msg=bad):
                af.parse_format(bad)
        self.assertEqual(af.parse_format("96000X8"), F96x8)
        self.assertEqual(af.parse_format("baseline"), BASELINE)
        self.assertEqual(af.parse_format("48000x2"), BASELINE)

    def test_times(self):
        self.assertEqual(af.times(1.4), "1.4×")
        self.assertEqual(af.times(12.3), "12×")

    def test_auto_picks_and_remembers_the_fastest(self):
        self.pi(works={F96x8})
        self.assertTrue(self.run_neg())
        self.assertEqual(self.p.format, F96x8)
        self.assertEqual(self.mem()["best"], F96x8.to_json())

    def test_set_from_another_format_switches_once(self):
        self.pi(works={F192x8, F96x8}, current=F192x8)
        self.neg.store.save("s1", {"best": F192x8.to_json()})
        self.assertTrue(self.run_neg(target=F96x8, chosen=True))
        self.assertEqual(self.p.format, F96x8)
        self.assertEqual(self.p.rebinds, 1)             # no detour through the baseline
        self.assertEqual(self.mem()["best"], F192x8.to_json())  # a choice isn't the fastest

    def test_failed_set_keeps_the_best(self):
        self.pi(works={F192x8})
        self.neg.store.save("s1", {"best": F192x8.to_json()})
        self.assertFalse(self.run_neg(target=F96x2, chosen=True))
        self.assertEqual(self.p.format, BASELINE)
        self.assertEqual(self.p.rebinds, 1)
        self.assertEqual(self.mem()["best"], F192x8.to_json())
        self.assertIn("96000x2", self.mem()["failed"])

    def test_fixed_format_respects_the_cool_off(self):
        self.pi(works=set())
        self.assertFalse(self.run_neg(target=F96x8))
        self.assertEqual(self.p.rebinds, 1)
        self.assertFalse(self.run_neg(target=F96x8))    # a reconnect soon after: not again
        self.assertEqual(self.p.rebinds, 1)
        self.assertIn("didn't work a moment ago", self.neg.events[-1][1])
        self.p.works.add(F96x8)
        self.assertTrue(self.run_neg(target=F96x8, chosen=True))    # asked for: tried anyway
        self.assertEqual(self.p.rebinds, 2)

    def test_forget_wins_over_a_negotiation_under_way(self):
        self.pi(works={F96x8})
        ops = self.neg.local_ops()
        self.neg.cooling["192000x8"] = self.clock.monotonic() + 100
        self.p.on["audio.confirm"] = lambda: ops["audio.forget"]({})
        self.run_neg()
        self.assertEqual(self.mem(), {})
        self.assertEqual(self.neg.cooling, {})

    def test_resume_goes_back_to_the_fastest(self):
        self.pi(works={F96x8})
        self.assertTrue(self.run_neg())
        self.assertEqual(self.p.format, F96x8)
        self.run_neg(target=BASELINE, chosen=True)              # as `audiolink update` does
        self.assertEqual(self.p.format, BASELINE)
        self.assertTrue(self.neg.local_ops()["audio.resume"]({})["started"])
        self.assertTrue(wait(lambda: not self.neg.running))
        self.assertEqual(self.p.format, F96x8)
        self.neg.mode = "baseline"
        self.assertEqual(self.neg.local_ops()["audio.resume"]({}), {"started": False, "mode": "baseline"})

    def test_pi_lost_mid_change(self):
        self.pi(works={F96x8})

        def unplug():
            self.p.peer_alive = False
            raise RuntimeError("Pi disconnected")
        self.p.on["audio.confirm"] = unplug
        self.assertFalse(self.run_neg(target=F96x8, chosen=True))
        self.assertTrue(self.neg.lost)
        st = self.neg.local_ops()["audio.status"]({})
        self.assertTrue(st["lost"])
        with self.assertRaisesRegex(RuntimeError, "dropped off USB"):
            self.neg.local_ops()["audio.run"]({"sweep": True})


class Command(unittest.TestCase):
    """`audiolink audio`: exit status and messages."""

    def run_cmd(self, *argv, status=None, run_error=None):
        from audiolink.__main__ import main
        calls = []

        def call(op, args=None, port=None):
            calls.append(op)
            if op == "audio.run" and run_error:
                raise SystemExit(run_error)
            if op == "audio.status":
                return dict({"running": False, "connected": True, "link": BASELINE.to_json(),
                             "remembered": {}, "mode": "auto", "pi": None, "events": []}, **(status or {}))
            return {}
        out = io.StringIO()
        with mock.patch("audiolink.host.control.call", call), contextlib.redirect_stdout(out):
            code = main(["audio"] + list(argv))
        return code, out.getvalue(), calls

    def test_bad_format_refused_before_anything_happens(self):
        with self.assertRaises(SystemExit) as e:
            self.run_cmd("set", "0x0")
        self.assertIn("isn't an audio format", str(e.exception.code))

    def test_exit_status(self):
        self.assertEqual(self.run_cmd()[0], 0)
        code, out, _ = self.run_cmd(status={"connected": False})
        self.assertEqual((code, out), (1, "No Pi plugged in\n"))
        code, out, _ = self.run_cmd(status={"connected": False, "lost": True})
        self.assertIn("dropped off USB", out)
        self.assertEqual(self.run_cmd("set", "96000x8", status={"outcome": False})[0], 1)
        self.assertEqual(self.run_cmd("set", "96000x8", status={"outcome": True})[0], 0)

    def test_ratio_for_a_chosen_format(self):
        code, out, _ = self.run_cmd(status={"link": F96x8.to_json(), "remembered": {
            "baseline_goodput": 100, "goodputs": {"96000x8": 140}, "best": F192x8.to_json()}})
        self.assertIn("Link: 96 kHz × 8 channels (1.4× baseline)", out)
        self.assertIn("Fastest so far: 192 kHz", out)


class PiRefuses(unittest.TestCase):
    def test_unknown_format_leaves_the_gadget_alone(self):
        gadget = mock.Mock()
        gadget.available.return_value = True
        gadget.current_both.return_value = gadget.home.return_value = (BASELINE, BASELINE)
        gadget.fits.return_value = True
        svc = AudioFormatService(mock.Mock(), gadget)
        for bad in ({"rate": 0, "channels": 0}, {"rate": 48000, "channels": 200}, {"rate": "x"}, None):
            with self.assertRaisesRegex(ValueError, "doesn't run audio format"):
                svc.handle("switch", {"format": bad})
        gadget.set_format.assert_not_called()


class CloneHome(unittest.TestCase):
    """A Pi whose device is a clone: home is its own formats (stereo out, mono in)."""
    MONO, STEREO = AudioFormat(48000, 1), AudioFormat(48000, 2)

    def service(self):
        gadget = mock.Mock()
        gadget.available.return_value = True
        gadget.home.return_value = gadget.current_both.return_value = (self.MONO, self.STEREO)
        gadget.fits.side_effect = lambda f: f.bytes_per_sec / 1000 + f.frame_bytes <= 1023
        audio = mock.Mock()
        audio.format, audio.record_format = self.MONO, self.STEREO
        return AudioFormatService(audio, gadget), gadget, audio

    def test_hello_and_no_reset_at_home(self):
        svc, gadget, _ = self.service()
        gadget.set_format.assert_not_called()              # already home: a clone stays a clone
        audio = svc.hello()["audio"]
        self.assertEqual(audio["home"], {"play": self.MONO.to_json(), "record": self.STEREO.to_json()})
        self.assertNotIn({"rate": 192000, "channels": 8}, audio["candidates"])
        self.assertIn({"rate": 96000, "channels": 4}, audio["candidates"])
        with self.assertRaisesRegex(ValueError, "doesn't run audio format"):
            svc.handle("switch", {"format": {"rate": 192000, "channels": 8}})

    def test_going_home(self):
        svc, gadget, audio = self.service()
        audio.format, audio.record_format = AudioFormat(96000, 4), None
        self.assertFalse(svc.at_home())
        with svc.lock:
            svc._switch("home")
        gadget.set_format.assert_called_with("home")
        audio.start.assert_called_with(self.MONO, self.STEREO)

    def test_host_side(self):
        from audiolink.host.audio_format import at_home, home_name
        home = {"play": self.MONO.to_json(), "record": self.STEREO.to_json()}
        self.assertTrue(at_home({"pi": {"format": self.MONO.to_json(), "home": home}, "link": self.STEREO.to_json()}))
        self.assertFalse(at_home({"pi": {"format": {"rate": 96000, "channels": 4}, "home": home}, "link": {}}))
        self.assertTrue(at_home({"pi": {}, "link": BASELINE.to_json()}))            # a Pi from before
        self.assertEqual(home_name((self.MONO, self.STEREO)), "48 kHz stereo out, 48 kHz mono in")
        self.assertEqual(home_name((BASELINE, BASELINE)), "48 kHz stereo")


if __name__ == "__main__":
    unittest.main()
