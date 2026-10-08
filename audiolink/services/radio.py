"""Pi side: the one owner of the Wi-Fi chip.

The BCM43430 is one radio shared by wlan0 (the Pi's Wi-Fi), uap0 (the setup network)
and wlan1 (a trial connection), with rules about what can run at once (services/
setup_ap.py, services/wifi_trial.py). Everything that changes the radio's state runs
as a job on this one thread, one at a time: joins (from the host over USB or from the
network), trials, network switches, the setup network's rule, parking, restarting
wpa_supplicant. So no decision is made on state another operation is halfway through
changing, and nothing has to pause anything else: a job simply runs after the one
before it.

Reading state (status, the last scan, saved networks) doesn't need the radio thread.
A job must not wait for another job (run() from the radio thread just calls through).
"""
import concurrent.futures
import logging
import queue
import threading

log = logging.getLogger("audiolink")


class Radio:
    def __init__(self):
        self.jobs = queue.Queue()
        self.thread = threading.Thread(target=self._loop, name="radio", daemon=True)
        self.thread.start()

    def _loop(self):
        while True:
            fn, args, kwargs, fut = self.jobs.get()
            if not fut.set_running_or_notify_cancel():
                continue
            try:
                fut.set_result(fn(*args, **kwargs))
            except BaseException as e:      # reported to whoever waits; the thread goes on
                fut.set_exception(e)
                if not fut.waited:
                    log.warning("radio: %s failed: %s", getattr(fn, "__name__", fn), e)

    def submit(self, fn, *args, **kwargs):
        """Queue a job; returns a Future. A failure nobody waits for is logged."""
        return self._submit(fn, args, kwargs, waited=False)

    def run(self, fn, *args, **kwargs):
        """Run a job and wait for its result (or exception)."""
        return self._submit(fn, args, kwargs, waited=True).result()

    def _submit(self, fn, args, kwargs, waited):
        fut = concurrent.futures.Future()
        fut.waited = waited
        if threading.current_thread() is self.thread:
            # From a job: queueing behind ourselves and waiting would never finish.
            try:
                fut.set_result(fn(*args, **kwargs))
            except BaseException as e:
                if not waited:
                    log.warning("radio: %s failed: %s", getattr(fn, "__name__", fn), e)
                fut.set_exception(e)
            return fut
        self.jobs.put((fn, args, kwargs, fut))
        return fut

    def busy(self):
        return not self.jobs.empty()
