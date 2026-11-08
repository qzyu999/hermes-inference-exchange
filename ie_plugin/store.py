"""Plugin-owned status, controls, and a process-lifetime hardware lease."""

import json
import os
import tempfile
import time
from pathlib import Path


class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lease = None

    def read(self, name, default=None):
        try:
            return json.loads((self.directory / f"{name}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default

    def write(self, name, value):
        fd, temporary = tempfile.mkstemp(dir=self.directory, prefix=".ie-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(value, handle)
            # Windows scanners can briefly hold the destination without delete sharing.
            deadline = time.monotonic() + 0.2
            while True:
                try:
                    os.replace(temporary, self.directory / f"{name}.json")
                    break
                except PermissionError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.01)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def acquire(self):
        handle = (self.directory / "provider.lock").open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                if handle.seek(0, os.SEEK_END) == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self.lease = handle
        return True

    def release(self):
        if self.lease:
            self.lease.close()
            self.lease = None

    def status(self):
        data = self.read("status", {})
        if time.time() - data.get("updated", 0) > 30:
            return "disabled (no active Hermes CLI controller)"
        return data.get("state", "disabled") + (
            f": {data['message']}" if data.get("message") else ""
        )
