#!/usr/bin/env python3
"""weightless lane watchdog — container-level self-healing for the rig.

The failure class this closes (TROUBLESHOOTING.md "The model is down", item
0): the serving stack dies cleanly — GPU work hangs, the wedged rank exits 0,
the head follows with EngineDeadError, everything exits 0. The hardware
watchdog, panic sysctls, and docker restart policies all stay silent because
nothing is wrong with the OS. The box is fine; only the stack is down.

A cron-run check (every 5 min) probes the current lane's /v1/models and, after
three consecutive failures, relaunches the lane through its start script —
never `docker start`, because RoCE GID indices re-resolve at launch. Lane
boots are long (15-40 min), so a relaunch arms a boot-grace lock that keeps
later cron ticks from piling on, and an external switch lock (touched by
`weightless.py serve` while it parks/boots lanes) suppresses the watchdog
during deliberate switches.

Install on the head node:  python3 lane_watchdog.py --install
Manual check:              python3 lane_watchdog.py --once --verbose
Stdlib only. Logs to ~/lane-watchdog.log.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request

HOME = os.path.expanduser("~")
STATE_DIR = os.environ.get("WEIGHTLESS_STATE_DIR", os.path.join(HOME, ".cache", "weightless"))
STATE = os.path.join(STATE_DIR, "lane-watchdog.json")
SWITCH_LOCK = os.path.join(STATE_DIR, "lane-switch.lock")
CURRENT_LANE = os.path.join(HOME, "current-lane")
LOG = os.path.join(HOME, "lane-watchdog.log")

# Operational lanes on the rig, keyed by ~/current-lane. The start script is
# the only supported relaunch path (GID re-resolution); port is where the
# lane's OpenAI server listens on the head. boot_s is the grace period after
# a relaunch during which the watchdog will not fire again. Keep in sync with
# scripts/lane-autostart.sh (the @reboot hook uses the same map).
LANES = {
    "dsv4": dict(port=8888, start=os.path.join(HOME, "dspark-miaai/start-deepseek-v4-flash-dspark.sh"),
                 boot_s=2400),
    "dsv4vl": dict(port=8888, start=os.path.join(HOME, "dspark-dsv4vl/start-dsv4-vl-dspark.sh"),
                   boot_s=2400),
    "glm53tp4": dict(port=8888, start=os.path.join(HOME, "tp4-bringup/launch-tp4.sh"),
                     boot_s=2700),
}

FAIL_THRESHOLD = 3  # 3 consecutive dead checks at */5 cron = ~10-15 min of outage


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}"
    try:
        with open(LOG, "a") as fh:
            fh.write(line + "\n")
    except OSError:
        pass
    if os.environ.get("WEIGHTLESS_WATCHDOG_VERBOSE"):
        print(line)


def load_state(path: str | None = None) -> dict:
    try:
        with open(path or STATE) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_state(state: dict, path: str | None = None) -> None:
    path = path or STATE
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(state, fh)
    os.replace(tmp, path)


def current_lane(path: str | None = None) -> str | None:
    try:
        with open(path or CURRENT_LANE) as fh:
            return fh.read().strip() or None
    except OSError:
        return None


def probe(port: int, timeout: float = 8.0) -> bool:
    """Healthy = /v1/models answers with at least one model id."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/models",
                                    timeout=timeout) as r:
            return '"id"' in r.read().decode()
    except Exception:
        return False


def lock_fresh(now: float, path: str | None = None, ttl: float = 0.0) -> bool:
    try:
        return now - os.path.getmtime(path or SWITCH_LOCK) < ttl
    except OSError:
        return False


def relaunch(lane_key: str, lane: dict, runner=subprocess.Popen) -> None:
    """Fire the lane's start script detached; the boot-grace state (set by the
    caller) keeps later ticks from stacking a second launch on top."""
    log(f"relaunching lane {lane_key}: {lane['start']}")
    with open(LOG, "a") as fh:
        runner(["bash", lane["start"]], stdout=fh, stderr=subprocess.STDOUT,
               start_new_session=True)


def decide(lane_key: str | None, healthy: bool, state: dict, *,
           switch_locked: bool, now: float, lane: dict | None,
           threshold: int = FAIL_THRESHOLD) -> str:
    """Pure state machine — one of: noop-unknown, skip-lock, ok, wait,
    in-flight, relaunch."""
    if lane_key is None or lane is None:
        return "noop-unknown"
    if switch_locked:
        return "skip-lock"
    if healthy:
        return "ok"
    if now - state.get("last_relaunch", 0.0) < lane["boot_s"]:
        return "in-flight"
    if state.get("failures", 0) + 1 < threshold:
        return "wait"
    return "relaunch"


def once(now: float, prober=probe, runner=subprocess.Popen,
         lane_key: str | None = None) -> str:
    lane_key = lane_key if lane_key is not None else current_lane()
    lane = LANES.get(lane_key or "")
    state = load_state()
    healthy = prober(lane["port"]) if lane else False
    switch_locked = lock_fresh(now, ttl=max((l["boot_s"] for l in LANES.values()), default=0))
    action = decide(lane_key, healthy, state, switch_locked=switch_locked,
                    now=now, lane=lane)
    if action == "ok":
        if state.get("failures"):
            log(f"lane {lane_key} healthy again")
        save_state({**state, "failures": 0})
    elif action == "wait":
        save_state({**state, "failures": state.get("failures", 0) + 1})
        log(f"lane {lane_key} endpoint dead (fail {state.get('failures', 0) + 1}/{FAIL_THRESHOLD})")
    elif action == "relaunch":
        relaunch(lane_key, lane, runner)
        save_state({**state, "failures": 0, "last_relaunch": now})
    elif action == "in-flight":
        log(f"lane {lane_key} still dead but a relaunch is in its boot grace")
    elif action == "noop-unknown":
        log(f"nothing to guard (current-lane: {lane_key or 'unset'})")
    return action


CRON_WATCHDOG = "*/5 * * * * $HOME/.local/bin/weightless-lane-watchdog.py --once"
CRON_AUTOSTART = "@reboot $HOME/.local/bin/lane-autostart.sh"


def install(here: str, runner=subprocess.run) -> int:
    """Idempotent: stage both self-healing scripts in ~/.local/bin and ensure
    their cron lines exist (watchdog every 5 min, autostart at boot)."""
    import shutil
    dest_dir = os.path.join(HOME, ".local", "bin")
    os.makedirs(dest_dir, exist_ok=True)
    for src, name in ((here, "weightless-lane-watchdog.py"),):
        dest = os.path.join(dest_dir, name)
        if os.path.abspath(src) != os.path.abspath(dest):
            shutil.copyfile(src, dest)
        os.chmod(dest, 0o755)
    autostart_src = os.path.join(os.path.dirname(os.path.abspath(here)),
                                 "lane-autostart.sh")
    if os.path.exists(autostart_src):
        dest = os.path.join(dest_dir, "lane-autostart.sh")
        if os.path.abspath(autostart_src) != os.path.abspath(dest):
            shutil.copyfile(autostart_src, dest)
        os.chmod(dest, 0o755)
    current = runner(["crontab", "-l"], capture_output=True, text=True)
    lines = (current.stdout or "").splitlines()
    add = []
    if not any("lane-watchdog" in l or "weightless-lane-watchdog" in l for l in lines):
        add.append(CRON_WATCHDOG)
    if os.path.exists(autostart_src) and not any("lane-autostart" in l for l in lines):
        add.append(CRON_AUTOSTART)
    if add:
        lines += add
        runner(["crontab", "-"], input="\n".join(lines) + "\n", text=True, check=True)
    print(f"installed: {dest_dir}/weightless-lane-watchdog.py"
          + (", lane-autostart.sh" if os.path.exists(autostart_src) else "")
          + ("; cron updated" if add else "; cron already armed"))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Probe the current lane and relaunch it when dead.",
                                 allow_abbrev=False)
    ap.add_argument("--once", action="store_true", help="single check (cron mode)")
    ap.add_argument("--install", action="store_true", help="stage scripts + arm cron, idempotent")
    ap.add_argument("--lane", help="guard this lane instead of ~/current-lane")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)
    if args.verbose:
        os.environ["WEIGHTLESS_WATCHDOG_VERBOSE"] = "1"
    if args.install:
        return install(os.path.abspath(__file__))
    action = once(time.time(), lane_key=args.lane)
    if args.verbose and action != "ok":
        print(f"action: {action}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
