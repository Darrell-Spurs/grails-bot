"""Local development runner: starts bot.py and restarts it when the code changes.

Production does not need this -- run bot.py directly (on a host, make bot.py
the startup file). Settings, from the environment or .env:

    WATCHDOG=0              run bot.py once: no watching, no restarts
    WATCHDOG_DEBOUNCE=1.5   seconds with no further changes before a restart

A restart waits until no .py file has changed for WATCHDOG_DEBOUNCE seconds, so
one save that touches several files -- a git pull, "Save All", a formatter
rewriting a file just after the editor wrote it -- restarts once, not once per
file event. A file whose contents did not actually change (OneDrive touching a
synced file, an editor re-saving identical bytes) is ignored. And the changed
files are compiled first: a save with a syntax error leaves the running bot up
and prints the error, instead of killing it for a replacement that cannot start.
"""
import hashlib
import os
import py_compile
import subprocess
import sys
import threading

from dotenv import load_dotenv

PROJECT = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(PROJECT, ".env"))

COMMAND = [sys.executable, "bot.py"]
WATCHDOG = os.environ.get("WATCHDOG", "1").strip().lower() not in ("0", "false", "no", "off")
DEBOUNCE = float(os.environ.get("WATCHDOG_DEBOUNCE", "1.5"))
IGNORED_DIRS = {".git", "__pycache__", ".venv", "venv", "testing", "logs", ".claude", "node_modules"}


def _digest(path):
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None     # deleted or mid-write: treat as changed


def _python_files():
    for folder, dirs, files in os.walk(PROJECT):
        dirs[:] = [d for d in dirs if d not in IGNORED_DIRS]
        for name in files:
            if name.endswith(".py"):
                yield os.path.join(folder, name)


class Restarter:
    """Runs the bot and restarts it after a burst of real changes goes quiet."""

    def __init__(self):
        self.hashes = {path: _digest(path) for path in _python_files()}
        self.pending = set()
        self.timer = None
        self.lock = threading.Lock()
        self.process = subprocess.Popen(COMMAND, cwd=PROJECT)

    def changed(self, path):
        if not path.endswith(".py"):
            return
        parts = set(os.path.relpath(path, PROJECT).split(os.sep))
        if parts & IGNORED_DIRS:
            return
        with self.lock:
            self.pending.add(path)
            if self.timer:
                self.timer.cancel()
            self.timer = threading.Timer(DEBOUNCE, self._settle)
            self.timer.daemon = True
            self.timer.start()

    def _settle(self):
        with self.lock:
            paths, self.pending = self.pending, set()
        real = []
        for path in paths:
            digest = _digest(path)
            if digest != self.hashes.get(path):
                self.hashes[path] = digest
                real.append(path)
        if not real:
            return

        broken = []
        for path in real:
            if not os.path.exists(path):
                continue
            try:
                py_compile.compile(path, doraise=True)
            except py_compile.PyCompileError as exc:
                broken.append(exc.msg)
        if broken:
            print("⚠️  Not restarting -- this does not compile, the running bot stays up:")
            for message in broken:
                print("   ", message.strip())
            return

        names = ", ".join(sorted(os.path.relpath(p, PROJECT) for p in real))
        print(f"🔁 Restarting bot ({names})...")
        self.process.kill()
        self.process.wait()
        self.process = subprocess.Popen(COMMAND, cwd=PROJECT)

    def stop(self):
        if self.timer:
            self.timer.cancel()
        self.process.kill()


def main():
    if not WATCHDOG:
        # Production-style: one bot process, nothing watching. bot.py itself is
        # what a host should start; this path is for WATCHDOG=0 locally.
        sys.exit(subprocess.call(COMMAND, cwd=PROJECT))

    # Imported only when watching, so a machine without the (dev-only)
    # watchdog package can still run WATCHDOG=0.
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer

    restarter = Restarter()

    class Handler(FileSystemEventHandler):
        def on_modified(self, event):
            if not event.is_directory:
                restarter.changed(event.src_path)

        on_created = on_deleted = on_modified

        def on_moved(self, event):
            if not event.is_directory:
                restarter.changed(event.dest_path)

    observer = Observer()
    observer.schedule(Handler(), path=PROJECT, recursive=True)
    observer.start()
    print(f"Watching for changes (restart after {DEBOUNCE:g}s of quiet; WATCHDOG=0 turns this off)...")
    try:
        # Joined in short steps: on Windows a join with no timeout cannot be
        # interrupted, so Ctrl+C would never land.
        while observer.is_alive():
            observer.join(1)
    except KeyboardInterrupt:
        pass
    finally:
        observer.stop()
        restarter.stop()


if __name__ == "__main__":
    main()
