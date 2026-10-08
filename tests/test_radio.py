"""services/radio.py: one radio job at a time, in order; results and errors come back."""
import threading
import time
import unittest

from audiolink.services.radio import Radio


class OneAtATime(unittest.TestCase):
    def test_jobs_never_overlap_and_keep_their_order(self):
        radio, running, overlaps, order = Radio(), [0], [], []

        def job(i):
            running[0] += 1
            if running[0] > 1:
                overlaps.append(i)
            time.sleep(0.01)
            order.append(i)
            running[0] -= 1
            return i

        threads = [threading.Thread(target=radio.run, args=(job, i)) for i in range(10)]
        for t in threads:
            t.start()
            time.sleep(0.002)
        for t in threads:
            t.join()
        self.assertEqual(overlaps, [])
        self.assertEqual(order, sorted(order))

    def test_results_errors_and_jobs_from_jobs(self):
        radio = Radio()
        self.assertEqual(radio.run(lambda: 42), 42)
        with self.assertRaises(ValueError):
            radio.run(lambda: int("x"))
        # A job that runs another job doesn't wait for itself.
        self.assertEqual(radio.run(lambda: radio.run(lambda: "inner")), "inner")
        self.assertEqual(radio.run(lambda: 1), 1)          # still working after an error


if __name__ == "__main__":
    unittest.main()
