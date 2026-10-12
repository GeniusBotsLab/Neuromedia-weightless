"""State-machine contracts for scripts/lane_watchdog.py.

The watchdog exists for the application-wedge failure class: the stack dies
cleanly (exit 0) while the box stays healthy, so only an endpoint probe can
tell. The contracts that matter: it must not fire during a deliberate switch
or a boot in progress, must not loop relaunches, and must recover the counter
when the lane comes back.
"""
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


wd = load("lane_watchdog", ROOT / "scripts" / "lane_watchdog.py")

LANE = {"port": 8888, "start": "/home/u/tp4-bringup/launch-tp4.sh", "boot_s": 2400.0}
NOW = 1_000_000.0


class DecideTests(unittest.TestCase):
    def test_healthy_resets(self):
        self.assertEqual(wd.decide("glm53tp4", True, {"failures": 2},
                                   switch_locked=False, now=NOW, lane=LANE), "ok")

    def test_first_failures_wait(self):
        for prior in (0, 1):
            self.assertEqual(wd.decide("glm53tp4", False, {"failures": prior},
                                       switch_locked=False, now=NOW, lane=LANE), "wait")

    def test_threshold_relaunches(self):
        self.assertEqual(wd.decide("glm53tp4", False, {"failures": 2},
                                   switch_locked=False, now=NOW, lane=LANE), "relaunch")

    def test_switch_lock_suppresses(self):
        self.assertEqual(wd.decide("glm53tp4", False, {"failures": 99},
                                   switch_locked=True, now=NOW, lane=LANE), "skip-lock")

    def test_boot_grace_suppresses_relaunch_loop(self):
        state = {"failures": 5, "last_relaunch": NOW - 60}
        self.assertEqual(wd.decide("glm53tp4", False, state,
                                   switch_locked=False, now=NOW, lane=LANE), "in-flight")

    def test_boot_grace_expired_allows_retry(self):
        state = {"failures": 5, "last_relaunch": NOW - 2500}
        self.assertEqual(wd.decide("glm53tp4", False, state,
                                   switch_locked=False, now=NOW, lane=LANE), "relaunch")

    def test_unknown_lane_is_noop(self):
        self.assertEqual(wd.decide("mystery", False, {}, switch_locked=False,
                                   now=NOW, lane=None), "noop-unknown")
        self.assertEqual(wd.decide(None, False, {}, switch_locked=False,
                                   now=NOW, lane=None), "noop-unknown")


class OnceTests(unittest.TestCase):
    """End-to-end through state files, with probe and launcher faked."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = os.path.join(self.tmp.name, "state.json")
        self.launched = []

    def fake_runner(self, argv, **kw):
        self.launched.append(argv)

        class P:
            pass
        return P()

    def test_failure_count_persists_then_relaunch_once(self):
        unhealthy = lambda port: False
        with unittest.mock.patch.multiple(wd, STATE=self.state):
            for expected in ("wait", "wait", "relaunch"):
                action = wd.once(NOW, prober=unhealthy, runner=self.fake_runner,
                                 lane_key="glm53tp4")
                self.assertEqual(action, expected)
            # Boot grace: further dead checks do not stack launches.
            for _ in range(3):
                action = wd.once(NOW + 60, prober=unhealthy, runner=self.fake_runner,
                                 lane_key="glm53tp4")
                self.assertEqual(action, "in-flight")
        self.assertEqual(len(self.launched), 1)
        self.assertEqual(self.launched[0], ["bash", wd.LANES["glm53tp4"]["start"]])

    def test_recovery_resets_counter(self):
        with unittest.mock.patch.multiple(wd, STATE=self.state):
            wd.once(NOW, prober=lambda port: False, runner=self.fake_runner,
                    lane_key="glm53tp4")
            action = wd.once(NOW + 300, prober=lambda port: True,
                             runner=self.fake_runner, lane_key="glm53tp4")
            self.assertEqual(action, "ok")
            self.assertEqual(json.load(open(self.state))["failures"], 0)


import unittest.mock  # noqa: E402  (used by OnceTests, kept close)


class InstallTests(unittest.TestCase):
    def test_install_is_idempotent(self):
        cron = {"text": ""}

        def fake_run(argv, **kw):
            if argv == ["crontab", "-l"]:
                class R:
                    stdout = cron["text"]
                    returncode = 0
                return R()
            if argv == ["crontab", "-"]:
                cron["text"] = kw["input"]

                class R:
                    returncode = 0
                return R()
            raise AssertionError(argv)

        with tempfile.TemporaryDirectory() as home:
            staged = os.path.join(home, "src")
            os.makedirs(staged)
            for name in ("lane_watchdog.py", "lane-autostart.sh"):
                Path(staged, name).write_text("# stub\n")
            with unittest.mock.patch.multiple(wd, HOME=home):
                wd.install(os.path.join(staged, "lane_watchdog.py"), runner=fake_run)
                first = cron["text"]
                wd.install(os.path.join(staged, "lane_watchdog.py"), runner=fake_run)
                self.assertEqual(cron["text"], first)  # second run adds nothing
        self.assertIn("weightless-lane-watchdog.py --once", first)
        self.assertIn("lane-autostart.sh", first)
        self.assertEqual(first.count("--once"), 1)


if __name__ == "__main__":
    unittest.main()
