"""Idle lifecycle. All launch and drain operations share one lock."""

import json
import logging
import os
import queue
import signal
import subprocess
import threading
import time
from dataclasses import asdict
from pathlib import Path

LOG = logging.getLogger("inference-exchange")


def child_environment(token):
    # The provider must never inherit hosted API keys or Hermes approval settings.
    allowed = {
        "PATH",
        "SYSTEMROOT",
        "WINDIR",
        "HOME",
        "USERPROFILE",
        "TMP",
        "TEMP",
        "TMPDIR",
        "LANG",
        "LC_ALL",
        "LD_LIBRARY_PATH",
        "DYLD_LIBRARY_PATH",
    }
    env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    env.update(PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8", IE_PROVIDER_TOKEN=token)
    return env


class Controller:
    def __init__(self, config, store, token="", clock=time.monotonic, launch=subprocess.Popen):
        self.config, self.store, self.token = config, store, token
        self.clock, self.launch = clock, launch
        self.lock = threading.RLock()
        self.quit = threading.Event()
        self.events = queue.Queue()
        self.process = None
        self.thread = None
        self.state = "disabled"
        self.message = ""
        self.last_activity = clock()
        self.started = 0
        self.retry_at = 0
        self.backoff = 5
        self.turns = set()

    def publish(self):
        self.store.write(
            "status",
            {
                "state": self.state,
                "message": self.message,
                "updated": time.time(),
            },
        )

    def error(self, message):
        self.state, self.message = "error", message
        self.retry_at = self.clock() + self.backoff
        self.backoff = min(self.backoff * 2, 120)
        LOG.warning("IE: %s; retry after idle backoff", message)

    def begin(self):
        with self.lock:
            if self.thread or not self.config.sell_enabled:
                return
            if not self.store.acquire():
                LOG.warning("IE: another CLI controller owns this profile")
                return
            self.state = "idle-waiting"
            self.thread = threading.Thread(target=self.run, name="ie-idle", daemon=True)
            self.thread.start()

    def run(self):
        try:
            while not self.quit.wait(0.25):
                try:
                    self.tick()
                except Exception as exc:
                    with self.lock:
                        self.stop_process()
                        self.error(
                            f"Controller failed ({type(exc).__name__}). "
                            "Check local provider configuration."
                        )
        finally:
            with self.lock:
                self.stop_process()
                self.state, self.message = "disabled", ""
                try:
                    self.publish()
                finally:
                    self.store.release()

    def read_events(self, process):
        try:
            for line in process.stdout:
                if line.startswith("IE_EVENT:"):
                    event = line.strip().removeprefix("IE_EVENT:")
                    if event in {"serving", "error"}:
                        self.events.put((process, event))
        except (OSError, ValueError):
            pass

    def tick(self):
        with self.lock:
            paused = self.store.read("paused", False)
            while not self.events.empty():
                process, event = self.events.get_nowait()
                if process is not self.process:
                    continue
                if event == "serving":
                    self.state, self.message = "serving", ""
                    self.backoff = 5
                else:
                    self.stop_process()
                    self.error("Coordinator unavailable or provider failed.")
            if self.process and self.process.poll() is not None:
                self.stop_process()
                self.error("Provider exited. Check model, dependencies, and coordinator.")
            if self.process and self.state == "starting":
                if self.clock() - self.started >= self.config.startup_seconds:
                    self.stop_process()
                    self.error("Provider startup timed out. Check model and coordinator.")
            if paused:
                self.stop_process()
                self.state, self.message = "disabled", "paused"
            elif self.state == "disabled":
                self.state, self.message = "idle-waiting", ""
                self.last_activity = self.clock()
            if (
                not paused
                and not self.turns
                and not self.process
                and self.clock() - self.last_activity >= self.config.idle_minutes * 60
                and self.clock() >= self.retry_at
            ):
                self.start_process()
            self.publish()

    def start_process(self):
        runner = Path(__file__).with_name("runner.py")
        kwargs = (
            {"start_new_session": True}
            if os.name != "nt"
            else {
                "creationflags": subprocess.CREATE_NO_WINDOW,
            }
        )
        self.state, self.message = "starting", ""
        self.started = self.clock()
        try:
            self.process = self.launch(
                [self.config.ie_python, str(runner)],
                cwd=self.config.ie_repo,
                env=child_environment(self.token),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                **kwargs,
            )
            self.process.stdin.write(json.dumps(asdict(self.config)) + "\n")
            self.process.stdin.flush()
            threading.Thread(
                target=self.read_events,
                args=(self.process,),
                daemon=True,
            ).start()
        except (OSError, ValueError):
            self.stop_process()
            self.error("Cannot launch provider. Check the separate IE Python environment.")

    def stop_process(self):
        process = self.process
        if process is None:
            return
        self.state = "draining"
        try:
            self.publish()
        except OSError:
            LOG.warning("IE: cannot write drain status")
        deadline = time.monotonic() + self.config.drain_seconds
        try:
            if process.poll() is None:
                try:
                    process.stdin.write("drain\n")
                    process.stdin.flush()
                    # Reserve time for forced process-tree cleanup.
                    process.wait(timeout=max(0.01, self.config.drain_seconds - 1))
                except (OSError, ValueError, subprocess.TimeoutExpired):
                    if os.name == "nt":
                        subprocess.run(
                            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            timeout=max(0.01, deadline - time.monotonic()),
                            creationflags=subprocess.CREATE_NO_WINDOW,
                        )
                    else:
                        os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=max(0.01, deadline - time.monotonic()))
        except (OSError, subprocess.TimeoutExpired):
            LOG.warning("IE: process cleanup could not be confirmed")
            raise RuntimeError("Provider cleanup failed") from None
        finally:
            if process.poll() is not None:
                for stream in (process.stdin, process.stdout):
                    if stream:
                        stream.close()
        self.process = None

    def activity(self, session_id="cli", turn_id="turn", **kwargs):
        self.begin()
        with self.lock:
            self.turns.add((session_id, turn_id))
            self.last_activity = self.clock()
            try:
                self.stop_process()
            except RuntimeError:
                self.error("Provider cleanup failed. Pause IE and check its process.")
                return {
                    "action": "block",
                    "message": "IE could not release local hardware.",
                }
            if self.thread:
                self.state, self.message = "idle-waiting", ""
                self.publish()

    def finished(self, session_id="cli", turn_id=None, **kwargs):
        with self.lock:
            self.turns = {
                turn
                for turn in self.turns
                if not (turn[0] == session_id and (turn_id is None or turn[1] == turn_id))
            }
            self.last_activity = self.clock()

    def session_start(self, **kwargs):
        self.begin()
        with self.lock:
            self.last_activity = self.clock()

    def close(self):
        self.quit.set()
        if self.thread:
            self.thread.join(timeout=self.config.drain_seconds + 1)
