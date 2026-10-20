#!/usr/bin/env python3
"""Run the test suite in parallel: one process per test module (python3 -m unittest
tests.MODULE), as many at once as there are CPUs, slowest-first from the last run's
times. Each module is its own process, so mocks, module state and sockets can't
collide. Prints each failing module's output, then one summary; exit status 1 if
anything failed. `python3 -m unittest discover -s tests -t .` still works, serially.
  tools/test.py [-j N] [MODULE...]       # MODULE: test_card, or tests.test_card
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TIMES = ROOT / "build" / "test-times.json"     # last run's seconds per module, for scheduling


def run(module):
    start = time.monotonic()
    p = subprocess.run([sys.executable, "-m", "unittest", "tests." + module], cwd=str(ROOT),
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    out = p.stdout.decode(errors="replace")
    m = re.search(r"^Ran (\d+) tests?", out, re.M)
    skipped = re.search(r"skipped=(\d+)", out.splitlines()[-1] if out.strip() else "")
    return module, p.returncode, out, int(m.group(1)) if m else 0, \
        int(skipped.group(1)) if skipped else 0, time.monotonic() - start


def main():
    a = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    a.add_argument("-j", type=int, default=os.cpu_count() or 4, help="modules at once (default: CPUs)")
    a.add_argument("modules", nargs="*")
    args = a.parse_args()
    modules = [m.replace("tests.", "") for m in args.modules] or \
        sorted(p.stem for p in (ROOT / "tests").glob("test_*.py"))
    try:
        times = json.loads(TIMES.read_text())
    except (OSError, ValueError):
        times = {}
    modules.sort(key=lambda m: -times.get(m, 0))
    start = time.monotonic()
    failed, total, skipped = [], 0, 0
    with ThreadPoolExecutor(max_workers=args.j) as pool:
        for f in as_completed([pool.submit(run, m) for m in modules]):
            module, status, out, n, skip, secs = f.result()
            times[module] = round(secs, 2)
            total, skipped = total + n, skipped + skip
            if status:
                failed.append(module)
                print("==== %s (%.1fs)\n%s" % (module, secs, out.rstrip()), flush=True)
    TIMES.parent.mkdir(exist_ok=True)
    TIMES.write_text(json.dumps(times, indent=1, sort_keys=True))
    print("Ran %d tests in %d modules, %.1fs (-j %d)%s" % (total, len(modules), time.monotonic() - start, args.j,
                                                         ", %d skipped" % skipped if skipped else ""))
    if failed:
        print("FAILED: %s" % ", ".join(sorted(failed)))
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
