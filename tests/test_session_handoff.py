#!/usr/bin/env python3
"""Shared desktop session handoff regressions with an isolated home and mock systemctl.

Real: Bash, GNU timeout, flock, elapsed time, marker files and concurrent callers.
Mocked: the user systemd manager, desktop launchers and os-session-select. No real systemd
commands, desktop sessions, host configuration changes or root are required.
Run: python3 -m unittest discover -s tests -p 'test_session_handoff.py' -v
"""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import unittest


REPO = Path(__file__).resolve().parents[1]
HELPER = REPO / "rootfs/usr/lib/skorion-session-handoff"
HELPER_PATH = "/usr/lib/skorion-session-handoff"
LAUNCHERS = ("gnome-session", "Hyprland", "startplasma-wayland", "startplasma-x11",
             "sx", "start-cosmic", "cinnamon-session-cinnamon")
TARGET = "graphical-session.target"
STEAM = "gamescope-session-plus@steam.service"
OTHER_GAME = "gamescope-session-plus@opengamepadui.service"
SENTINEL = "steamos-session-select"
RETURN_FILE = "skorionos-session-return"
RETURN_VALUE = "gamescope-session-steam\n"


def unit_record(unit, load="loaded", active="inactive", sub="dead", job=""):
    return (f"Id={unit}\nLoadState={load}\nActiveState={active}\n"
            f"SubState={sub}\nJob={job}\n")


def stopped_states(*extra):
    return "\n".join([unit_record(TARGET), unit_record(STEAM), *extra])


SHIM = r'''#!/usr/bin/env python3
import fcntl, json, os, pathlib, sys, time
root = pathlib.Path(os.environ["SESSION_TEST_ROOT"])
plan = json.loads((root / "plan.json").read_text())
name, args = pathlib.Path(sys.argv[0]).name, sys.argv[1:]

def event(kind, **data):
    data.update(event=kind, time=time.monotonic(), args=args)
    fd = os.open(root / "events", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, (json.dumps(data) + "\n").encode())
    finally:
        os.close(fd)

def count(key):
    path = root / (key + "-count")
    value = int(path.read_text()) + 1 if path.exists() else 1
    path.write_text(str(value))
    return value

def config_dir():
    path = pathlib.Path(os.environ.get("XDG_CONF_DIR") or pathlib.Path(os.environ["HOME"]) / ".config")
    return path if path.is_absolute() else pathlib.Path(os.environ["HOME"]) / path

def lock_fds():
    lock = config_dir() / "skorion-session-oneshot.lock"
    inherited = []
    for path in pathlib.Path("/proc/self/fd").iterdir():
        try:
            if path.resolve() == lock:
                inherited.append(path.name)
        except OSError:
            pass
    return inherited

if name == "systemctl":
    assert args and args[0] == "--user", "Only the mocked user manager is supported"
    command = args[1]
    assert command in ("show", "list-jobs"), args
    n = count(command)
    event(command, count=n)
    steps = plan.get(command, [{"output": ""}])
    step = steps[min(n - 1, len(steps) - 1)]
    if "after" in step:
        elapsed = time.monotonic() - float((root / "started").read_text())
        output = step["after_output"] if elapsed >= step["after"] else step["output"]
    else:
        output = step.get("output", "")
    time.sleep(step.get("delay", 0))
    print(output, end="")
    sys.exit(step.get("status", 0))
elif name in ("gnome-session", "Hyprland", "startplasma-wayland", "startplasma-x11", "sx", "start-cosmic", "cinnamon-session-cinnamon"):
    config = config_dir()
    lock = config / "skorion-session-oneshot.lock"
    inherited = lock_fds()
    with lock.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            unlocked = True
        except BlockingIOError:
            unlocked = False
    event("launch", binary=name, inherited_lock_fds=inherited, lock_available=unlocked,
          sentinel_exists=(config / "steamos-session-select").exists(),
          return_exists=(config / "skorionos-session-return").exists())
    time.sleep(plan.get("launch_sleep", 0))
    sys.exit(plan.get("launch_status", 0))
elif name == "os-session-select":
    n = count("restore")
    event("restore", count=n, inherited_lock_fds=lock_fds())
    statuses = plan.get("restore_statuses", [0])
    sys.exit(statuses[min(n - 1, len(statuses) - 1)])
elif name == "sudo":
    if args and args[0] == "systemd-cat":
        sys.stdin.buffer.read()
        event("log", inherited_lock_fds=lock_fds())
    elif args == ["skorion-session-use-sddm", "cinnamon"]:
        event("switch", inherited_lock_fds=lock_fds())
    else:
        raise AssertionError("Unexpected sudo command: " + repr(args))
    sys.exit(0)
elif name == "systemd-cat":
    sys.stdin.buffer.read()
    event("log", inherited_lock_fds=lock_fds())
    sys.exit(0)
elif name == "systemd-run":
    event("schedule", inherited_lock_fds=lock_fds())
    sys.exit(0)
elif name == "rm":
    event("remove")
    if plan.get("fail_marker_remove") and any(pathlib.Path(a).name == "steamos-session-select" for a in args):
        print("mock marker removal failed", file=sys.stderr)
        sys.exit(1)
    os.execv(os.environ["SESSION_REAL_RM"], ["rm", *args])
else:
    raise AssertionError(name)
'''


@unittest.skipUnless(all(shutil.which(tool) for tool in ("bash", "timeout", "flock")),
                     "Bash, GNU timeout and flock are required")
class SessionHandoffTestCase(unittest.TestCase):
    desktop = "gnome"
    session = "wayland"
    expected_launch = ("gnome-session",)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="session-handoff-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.home = self.root / "home"
        self.config = self.home / ".config"
        self.config.mkdir(parents=True)
        self.marker = self.config / SENTINEL
        self.return_file = self.config / RETURN_FILE
        self.marker.write_text("wayland\n")
        self.return_file.write_text(RETURN_VALUE)
        shim = self.bin / "shim"
        shim.write_text(SHIM)
        shim.chmod(0o755)
        for name in (*LAUNCHERS, "systemctl", "os-session-select", "rm", "sudo", "systemd-cat", "systemd-run"):
            (self.bin / name).symlink_to(shim)
        self.script = self.root / (self.desktop + "-session-oneshot")
        self.helper = self.root / "skorion-session-handoff"
        self.plan = {"show": [{"output": stopped_states()}]}
        self.env = dict(os.environ, HOME=str(self.home), XDG_CONF_DIR=str(self.config),
                        PATH=str(self.bin) + os.pathsep + os.environ["PATH"],
                        SESSION_TEST_ROOT=str(self.root), SESSION_REAL_RM=shutil.which("rm"),
                        SK_CHOS_SESSION=self.session)
        self.write_script()

    def write_script(self, timeout=4):
        source, replacements = re.subn(r"^HANDOFF_TIMEOUT=30$", f"HANDOFF_TIMEOUT={timeout}",
                                       HELPER.read_text(), flags=re.MULTILINE)
        self.assertEqual(replacements, 1, "Expected one production handoff timeout")
        self.helper.write_text(source)
        self.write_wrapper(self.desktop, self.script)

    def write_wrapper(self, desktop, destination):
        source = (REPO / f"postcopy/{desktop}/usr/bin/{desktop}-session-oneshot").read_text()
        self.assertIn(HELPER_PATH, source)
        source = source.replace(HELPER_PATH, str(self.helper))
        for binary in LAUNCHERS:
            source = source.replace("/usr/bin/" + binary, str(self.bin / binary))
        source = source.replace("/usr/lib/os-session-select", str(self.bin / "os-session-select"))
        source = source.replace("/etc/default/sk-chos-desktop-session", str(self.root / "desktop-session.conf"))
        destination.write_text(source)

    def use_config(self, path, relative=False):
        new_config = self.home / path if relative else self.root / path
        new_config.parent.mkdir(parents=True, exist_ok=True)
        self.config.rename(new_config)
        self.config = new_config
        self.marker = self.config / SENTINEL
        self.return_file = self.config / RETURN_FILE
        self.env["XDG_CONF_DIR"] = str(path if relative else self.config)

    def prepare(self):
        (self.root / "plan.json").write_text(json.dumps(self.plan))
        (self.root / "started").write_text(str(time.monotonic()))

    def run_session(self, timeout=9):
        self.prepare()
        start = time.monotonic()
        result = subprocess.run(["bash", str(self.script)], env=self.env,
                                capture_output=True, text=True, timeout=timeout)
        self.elapsed = time.monotonic() - start
        for entry in self.events("log"):
            self.assertEqual(entry["inherited_lock_fds"], [], "Logger must not inherit the session lock")
        return result

    def events(self, kind=None):
        path = self.root / "events"
        entries = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        return [item for item in entries if kind is None or item["event"] == kind]

    def assert_launched(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        launches = self.events("launch")
        self.assertEqual(len(launches), 1, self.events())
        self.assertEqual(launches[0]["inherited_lock_fds"], [])
        self.assertTrue(launches[0]["lock_available"])
        return launches[0]

    def assert_blocked(self, result):
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.events("launch"), [])
        self.assertEqual(self.events("switch"), [])
        self.assertEqual(self.events("schedule"), [])


class GnomeSessionCoreTests(SessionHandoffTestCase):
    def test_quiet_session_launches_immediately_and_preserves_return(self):
        result = self.run_session()
        launch = self.assert_launched(result)
        self.assertLess(self.elapsed, 1.5, "No fixed two-second delay should remain")
        self.assertFalse(launch["sentinel_exists"])
        self.assertTrue(launch["return_exists"])
        self.assertEqual(self.return_file.read_text(), RETURN_VALUE)
        self.assertEqual(self.events("restore"), [])
        self.assertTrue((self.config / "skorion-session-oneshot.lock").exists())
        for entry in self.events("show"):
            self.assertIn(TARGET, entry["args"])
            self.assertIn(STEAM, entry["args"])
            self.assertIn("gamescope-session-plus@*.service", entry["args"])
        job_args = self.events("list-jobs")[0]["args"]
        for flag in ("--no-legend", "--plain", "--no-pager", "--full"):
            self.assertIn(flag, job_args)

    def test_logout_after_desktop_launch_restores_saved_game_session(self):
        self.assert_launched(self.run_session())
        lock = self.config / "skorion-session-oneshot.lock"
        lock_inode = lock.stat().st_ino
        query_count = len(self.events("show"))
        result = self.run_session()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.events("launch")), 1)
        self.assertEqual(len(self.events("show")), query_count)
        self.assertEqual([entry["args"] for entry in self.events("restore")], [[RETURN_VALUE.strip()]])
        self.assertEqual(self.events("restore")[0]["inherited_lock_fds"], [])
        self.assertFalse(self.return_file.exists())
        self.assertEqual(lock.stat().st_ino, lock_inode, "The lock file must never be unlinked")

    def test_service_teardown_can_outlast_two_seconds_and_target(self):
        busy = "\n".join([unit_record(TARGET), unit_record(STEAM, active="deactivating", sub="stop-sigterm")])
        self.plan["show"] = [{"output": busy, "after": 2.4, "after_output": stopped_states()}]
        result = self.run_session()
        self.assert_launched(result)
        self.assertGreaterEqual(self.elapsed, 2.4)
        self.assertGreater(len(self.events("show")), 2)
        self.assertLess(self.elapsed, 4.5)

    def test_target_teardown_is_required_even_when_service_is_dead(self):
        busy = "\n".join([unit_record(TARGET, active="deactivating", sub="stop"), unit_record(STEAM)])
        self.plan["show"] = [{"output": busy}, {"output": stopped_states()}]
        self.assert_launched(self.run_session())
        self.assertEqual(len(self.events("show")), 2)

    def test_other_gamescope_instance_must_be_stopped(self):
        busy = stopped_states(unit_record(OTHER_GAME, active="active", sub="running"))
        self.plan["show"] = [{"output": busy}, {"output": stopped_states(unit_record(OTHER_GAME))}]
        self.assert_launched(self.run_session())
        self.assertEqual(len(self.events("show")), 2)

    def test_duplicate_exact_and_glob_service_records_must_all_be_stopped(self):
        # systemctl can print steam once for its exact name and again for the glob.
        busy = stopped_states(unit_record(STEAM, active="deactivating", sub="stop"))
        self.plan["show"] = [{"output": busy}, {"output": stopped_states(unit_record(STEAM))}]
        self.assert_launched(self.run_session())
        self.assertEqual(len(self.events("show")), 2)
        self.assertEqual(len(self.events("list-jobs")), 1)

    def test_every_related_job_type_blocks_until_queue_clears(self):
        kinds = ("start", "stop", "restart", "reload", "try-restart", "reload-or-start")
        self.plan["list-jobs"] = [{"output": f"{i + 1} {TARGET if i % 2 else OTHER_GAME} {kind} waiting\n"}
                                  for i, kind in enumerate(kinds)] + [{"output": ""}]
        self.assert_launched(self.run_session())
        self.assertEqual(len(self.events("list-jobs")), len(kinds) + 1)
        self.assertGreater(self.events("launch")[0]["time"], self.events("list-jobs")[-1]["time"])

    def test_unrelated_jobs_do_not_block(self):
        self.plan["list-jobs"] = [{"output": "42 unrelated.service restart running\n43 graphical-session-pre.target start waiting\n"}]
        self.assert_launched(self.run_session())
        self.assertEqual(len(self.events("show")), 1)

    def test_unit_job_property_blocks_even_when_list_jobs_would_be_empty(self):
        pending = "\n".join([unit_record(TARGET, job="19"), unit_record(STEAM)])
        self.plan["show"] = [{"output": pending}, {"output": stopped_states()}]
        self.assert_launched(self.run_session())
        self.assertEqual(len(self.events("show")), 2)
        self.assertEqual(len(self.events("list-jobs")), 1)

    def test_missing_optional_service_is_safe_with_complete_dead_state(self):
        self.plan["show"] = [{"output": "\n".join([unit_record(TARGET), unit_record(STEAM, load="not-found")])}]
        self.assert_launched(self.run_session())

    def test_unsafe_and_incomplete_unit_states_fail_closed(self):
        bad_states = {
            "empty": "",
            "target_missing": unit_record(STEAM),
            "steam_missing": unit_record(TARGET),
            "id_missing": stopped_states().replace(f"Id={TARGET}\n", "", 1),
            "load_missing": stopped_states().replace("LoadState=loaded\n", "", 1),
            "active_missing": stopped_states().replace("ActiveState=inactive\n", "", 1),
            "substate_missing": stopped_states().replace("SubState=dead\n", "", 1),
            "job_missing": stopped_states().replace("Job=\n", "", 1),
            "failed_service": "\n".join([unit_record(TARGET), unit_record(STEAM, active="failed", sub="failed")]),
            "inactive_but_not_dead": "\n".join([unit_record(TARGET), unit_record(STEAM, sub="exited")]),
            "target_not_found": "\n".join([unit_record(TARGET, load="not-found"), unit_record(STEAM)]),
            "unknown_field": stopped_states() + "Unexpected=data\n",
            "malformed_record": "not property data\n",
        }
        self.write_script(timeout=1)
        for name, output in bad_states.items():
            with self.subTest(name=name):
                self.marker.write_text("wayland\n")
                self.plan["show"] = [{"output": output}]
                result = self.run_session()
                self.assert_blocked(result)
                self.assertFalse(self.marker.exists())
                self.assertEqual(self.return_file.read_text(), RETURN_VALUE)

    def test_bus_query_failure_is_not_inactive(self):
        self.write_script(timeout=1)
        self.plan["show"] = [{"status": 1, "output": ""}]
        self.assert_blocked(self.run_session())
        self.assertEqual(self.events("list-jobs"), [])

    def test_jobs_query_failure_and_malformed_output_fail_closed(self):
        self.write_script(timeout=1)
        for step in ({"status": 1}, {"output": "not a job response\n"}):
            with self.subTest(step=step):
                self.marker.write_text("wayland\n")
                self.plan["list-jobs"] = [step]
                self.assert_blocked(self.run_session())

    def test_hung_show_is_killed_and_retried_within_deadline(self):
        self.plan["show"] = [{"delay": 10}, {"output": stopped_states()}]
        self.assert_launched(self.run_session())
        self.assertGreaterEqual(self.elapsed, 1.8)
        self.assertLess(self.elapsed, 4.5)
        self.assertEqual(len(self.events("show")), 2)

    def test_hung_jobs_query_cannot_bypass_barrier(self):
        self.write_script(timeout=1)
        self.plan["list-jobs"] = [{"delay": 10}]
        self.assert_blocked(self.run_session(timeout=4))
        self.assertLess(self.elapsed, 2.5)

    def test_deadline_stops_hung_queries_and_next_login_restores_return(self):
        self.write_script(timeout=3)
        self.plan["show"] = [{"delay": 10}]
        self.assert_blocked(self.run_session(timeout=6))
        self.assertLess(self.elapsed, 4.5)
        # The production clock intentionally uses whole monotonic seconds.
        self.assertGreaterEqual(self.elapsed, 2.0)
        self.assertFalse(self.marker.exists())
        self.assertEqual(self.return_file.read_text(), RETURN_VALUE)
        query_count = len(self.events("show"))
        result = self.run_session(timeout=3)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.events("launch"), [])
        self.assertEqual(len(self.events("show")), query_count)
        self.assertEqual(self.events("restore")[-1]["args"], [RETURN_VALUE.strip()])
        self.assertEqual(self.events("restore")[-1]["inherited_lock_fds"], [])
        self.assertFalse(self.return_file.exists())

    def test_restore_failure_fallback_also_waits_for_barrier(self):
        self.marker.unlink()
        self.plan["restore_statuses"] = [1, 1]
        busy = "\n".join([unit_record(TARGET), unit_record(STEAM, active="deactivating", sub="stop")])
        self.plan["show"] = [{"output": busy}, {"output": stopped_states()}]
        self.assert_launched(self.run_session())
        self.assertEqual([entry["args"] for entry in self.events("restore")], [[RETURN_VALUE.strip()], []])
        self.assertTrue(all(entry["inherited_lock_fds"] == [] for entry in self.events("restore")))
        self.assertEqual(len(self.events("show")), 2)

    def test_missing_return_fallback_never_launches_while_session_is_active(self):
        self.marker.unlink()
        self.return_file.unlink()
        self.plan["restore_statuses"] = [1]
        self.plan["show"] = [{"output": "\n".join([unit_record(TARGET), unit_record(STEAM, active="active", sub="running")])}]
        self.write_script(timeout=1)
        self.assert_blocked(self.run_session())
        self.assertTrue(self.events("restore"))

    def test_marker_removal_failure_cannot_launch_or_consume_return(self):
        self.plan["fail_marker_remove"] = True
        self.assert_blocked(self.run_session())
        self.assertTrue(self.marker.exists())
        self.assertEqual(self.return_file.read_text(), RETURN_VALUE)
        self.assertEqual(self.events("show"), [])
        self.assertEqual(self.events("restore"), [])

    def test_concurrent_invocation_cannot_consume_return_or_launch_twice(self):
        busy = "\n".join([unit_record(TARGET), unit_record(STEAM, active="deactivating", sub="stop")])
        self.plan["show"] = [{"output": busy, "after": 1.2, "after_output": stopped_states()}]
        self.prepare()
        first = subprocess.Popen(["bash", str(self.script)], env=self.env,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 3
            while not self.events("show") and first.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(self.events("show"), "First invocation did not reach the barrier")
            second = subprocess.run(["bash", str(self.script)], env=self.env,
                                    capture_output=True, text=True, timeout=2)
            self.assertNotEqual(second.returncode, 0, second.stdout + second.stderr)
            self.assertEqual(self.events("restore"), [])
            self.assertEqual(self.return_file.read_text(), RETURN_VALUE)
            stdout, stderr = first.communicate(timeout=6)
            self.assert_launched(subprocess.CompletedProcess(first.args, first.returncode, stdout, stderr))
        finally:
            if first.poll() is None:
                first.kill()
                first.communicate()

    def test_new_marker_written_during_wait_is_not_removed(self):
        busy = "\n".join([unit_record(TARGET), unit_record(STEAM, active="deactivating", sub="stop")])
        self.plan["show"] = [{"output": busy, "after": 0.8, "after_output": stopped_states()}]
        self.prepare()
        process = subprocess.Popen(["bash", str(self.script)], env=self.env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 3
            while not self.events("show") and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(self.events("show"), "Invocation did not reach the barrier")
            self.assertFalse(self.marker.exists())
            self.marker.write_text("a newer request\n")
            stdout, stderr = process.communicate(timeout=6)
            launch = self.assert_launched(subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr))
            self.assertTrue(launch["sentinel_exists"])
            self.assertEqual(self.marker.read_text(), "a newer request\n")
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()


class DesktopWrapperTests(SessionHandoffTestCase):
    """The same wrapper contract for each shipped desktop and session mode."""

    def assert_command(self, result, expected=None):
        launch = self.assert_launched(result)
        expected = expected or self.expected_launch
        self.assertEqual(launch["binary"], expected[0])
        self.assertEqual(launch["args"], [str(self.bin / arg) if arg in LAUNCHERS else arg
                                           for arg in expected[1:]])
        return launch

    def assert_success(self, result):
        if self.desktop == "cinnamon" and self.session == "xorg":
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(self.events("launch"), [])
            self.assertEqual(len(self.events("switch")), 1)
            self.assertEqual(len(self.events("schedule")), 1)
            for entry in self.events("switch") + self.events("schedule"):
                self.assertEqual(entry["inherited_lock_fds"], [])
                self.assertGreater(entry["time"], self.events("list-jobs")[-1]["time"])
            self.assertEqual(self.events("switch")[0]["args"], ["skorion-session-use-sddm", "cinnamon"])
            self.assertIn("--on-active=5s", self.events("schedule")[0]["args"])
            self.assertIn(RETURN_VALUE.strip(), self.events("schedule")[0]["args"][-1])
        else:
            self.assert_command(result)
        self.assertFalse(self.marker.exists())
        self.assertEqual(self.return_file.read_text(), RETURN_VALUE)
        self.assertTrue((self.config / "skorion-session-oneshot.lock").exists())

    def test_selected_command_waits_for_handoff(self):
        busy = "\n".join([unit_record(TARGET), unit_record(STEAM, active="deactivating", sub="stop")])
        self.plan["show"] = [{"output": busy}, {"output": stopped_states()}]
        self.assert_success(self.run_session())
        self.assertEqual(len(self.events("show")), 2)

    def test_saved_return_restores_without_desktop_start(self):
        self.marker.unlink()
        result = self.run_session()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(self.events("restore")), 1)
        self.assertEqual(self.events("restore")[0]["args"], [RETURN_VALUE.strip()])
        self.assertEqual(self.events("restore")[0]["inherited_lock_fds"], [])
        self.assertFalse(self.return_file.exists())
        for kind in ("launch", "switch", "schedule", "show"):
            self.assertEqual(self.events(kind), [])

    def test_restore_fallback_uses_selected_command_and_barrier(self):
        self.marker.unlink()
        self.plan["restore_statuses"] = [1, 1]
        busy = "\n".join([unit_record(TARGET), unit_record(STEAM, active="deactivating", sub="stop")])
        self.plan["show"] = [{"output": busy}, {"output": stopped_states()}]
        self.assert_command(self.run_session())
        self.assertEqual(len(self.events("show")), 2)
        self.assertEqual([entry["args"] for entry in self.events("restore")], [[RETURN_VALUE.strip()], []])
        self.assertTrue(all(entry["inherited_lock_fds"] == [] for entry in self.events("restore")))
        self.assertEqual(self.events("schedule"), [])
        self.assertEqual(self.events("switch"), [])

    def test_timeout_blocks_launch_and_side_effects_preserving_return(self):
        self.write_script(timeout=1)
        self.plan["show"] = [{"output": "\n".join([unit_record(TARGET), unit_record(STEAM, active="active", sub="running")])}]
        self.assert_blocked(self.run_session())
        self.assertFalse(self.marker.exists())
        self.assertEqual(self.return_file.read_text(), RETURN_VALUE)

    def test_missing_helper_fails_before_marker_changes(self):
        self.helper.unlink()
        self.assert_blocked(self.run_session())
        self.assertEqual(self.marker.read_text(), "wayland\n")
        self.assertEqual(self.return_file.read_text(), RETURN_VALUE)
        self.assertEqual(self.events("restore"), [])
        self.assertEqual(self.events("show"), [])

    def test_absolute_config_directory_with_spaces(self):
        self.use_config("custom config/settings")
        self.assert_success(self.run_session())
        self.assertFalse((self.home / ".config").exists())

    def test_relative_config_directory_is_resolved_from_home(self):
        self.use_config("relative config/settings", relative=True)
        self.assert_success(self.run_session())
        self.assertFalse((self.home / ".config").exists())

    def test_default_config_directory_when_override_is_unset(self):
        self.env.pop("XDG_CONF_DIR")
        self.assert_success(self.run_session())


class PlasmaWaylandTests(DesktopWrapperTests):
    desktop = "plasma"
    expected_launch = ("startplasma-wayland",)


class PlasmaX11Tests(DesktopWrapperTests):
    desktop = "plasma"
    session = "xorg"
    expected_launch = ("sx", "startplasma-x11")


class HyprlandTests(DesktopWrapperTests):
    desktop = "hyprland"
    expected_launch = ("Hyprland",)


class CosmicTests(DesktopWrapperTests):
    desktop = "cosmic"
    expected_launch = ("start-cosmic",)


class CinnamonWaylandTests(DesktopWrapperTests):
    desktop = "cinnamon"
    expected_launch = ("cinnamon-session-cinnamon", "--wayland")

    def test_non_wayland_marker_keeps_xorg_command_selection(self):
        self.marker.write_text("xorg\n")
        self.assert_command(self.run_session(), expected=("sx", "cinnamon-session-cinnamon"))
        self.assertFalse(self.marker.exists())


class CinnamonXorgTests(DesktopWrapperTests):
    desktop = "cinnamon"
    session = "xorg"
    expected_launch = ("sx", "cinnamon-session-cinnamon")


class SharedIntegrationTests(SessionHandoffTestCase):
    def test_different_desktops_share_the_same_lock(self):
        variants = (("plasma", "wayland"), ("plasma", "xorg"), ("hyprland", "wayland"),
                    ("cosmic", "wayland"), ("cinnamon", "wayland"), ("cinnamon", "xorg"))
        others = []
        for desktop, session in variants:
            script = self.root / f"other-{desktop}-{session}"
            self.write_wrapper(desktop, script)
            others.append((script, dict(self.env, SK_CHOS_SESSION=session)))
        busy = "\n".join([unit_record(TARGET), unit_record(STEAM, active="deactivating", sub="stop")])
        self.plan["show"] = [{"output": busy}]
        self.prepare()
        first = subprocess.Popen(["bash", str(self.script)], env=self.env,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 3
            while not self.events("show") and first.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(self.events("show"), "GNOME did not reach the barrier")
            for script, env in others:
                with self.subTest(wrapper=script.name):
                    result = subprocess.run(["bash", str(script)], env=env,
                                            capture_output=True, text=True, timeout=2)
                    self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertEqual(self.events("restore"), [])
                    self.assertEqual(self.events("launch"), [])
                    self.assertEqual(self.events("switch"), [])
                    self.assertEqual(self.events("schedule"), [])
                    self.assertEqual(self.return_file.read_text(), RETURN_VALUE)
            self.plan["show"] = [{"output": stopped_states()}]
            (self.root / "plan.json").write_text(json.dumps(self.plan))
            stdout, stderr = first.communicate(timeout=6)
            self.assert_launched(subprocess.CompletedProcess(first.args, first.returncode, stdout, stderr))
        finally:
            if first.poll() is None:
                first.kill()
                first.communicate()


class ManifestPackagingTests(unittest.TestCase):
    def test_all_eleven_active_manifests_preserve_and_ship_shared_helper(self):
        # Inspect the actual overlay order without running build/install hooks.
        manifests = sorted((REPO / "branch").glob("manifest-*"))
        self.assertEqual(len(manifests), 11)
        self.assertTrue(HELPER.is_file())
        self.assertTrue(HELPER.stat().st_mode & 0o444, "The sourced helper must be readable")
        self.assertIn("cp -rav postcopy/${dir}/* rootfs/", (REPO / "pre-build-image.sh").read_text())
        self.assertIn("cp -R rootfs/. ${BUILD_PATH}/", (REPO / "build-image.sh").read_text())
        for manifest in manifests:
            with self.subTest(manifest=manifest.name):
                match = re.search(r'export POSTCOPY="(.*?)"', manifest.read_text(), re.DOTALL)
                self.assertIsNotNone(match)
                overlays = match.group(1).replace("\\", " ").split()
                desktop = "plasma" if manifest.name.startswith("manifest-kde") else (
                    "gnome" if manifest.name.startswith("manifest-gnome") else "hyprland")
                self.assertIn(desktop, overlays)
                wrapper = None
                for overlay in overlays:
                    directory = REPO / "postcopy" / overlay
                    self.assertTrue(directory.is_dir(), str(directory))
                    for relative in ("usr", "usr/lib", "usr/lib/skorion-session-handoff"):
                        path = directory / relative
                        self.assertFalse(path.is_symlink(), f"Overlay redirects helper path: {path}")
                        self.assertFalse(path.is_file(), f"Overlay replaces helper path: {path}")
                    candidate = directory / "usr/bin" / f"{desktop}-session-oneshot"
                    if candidate.exists():
                        wrapper = candidate
                self.assertIsNotNone(wrapper)
                self.assertIn("source " + HELPER_PATH, wrapper.read_text())


if __name__ == "__main__":
    unittest.main()
