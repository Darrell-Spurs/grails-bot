# dev_runner.py
import subprocess
import time
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

class RestartOnChange(FileSystemEventHandler):
    def __init__(self, command):
        self.command = command
        self.process = subprocess.Popen(self.command)

    def restart(self):
        print("🔁 Restarting bot...")
        self.process.kill()
        self.process = subprocess.Popen(self.command)

    def on_modified(self, event):
        if event.src_path.endswith(".py"):
            self.restart()

if __name__ == "__main__":
    path = "."
    command = ["python", "bot.py"]

    event_handler = RestartOnChange(command)
    observer = Observer()
    observer.schedule(event_handler, path=path, recursive=True)
    observer.start()

    print("👀 Watching for changes...")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
        event_handler.process.kill()
    observer.join()
