import time
import os
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from services.ast_analyzer import ASTAnalyzer

class SincFileHandler(FileSystemEventHandler):
    """Handles file system events by triggering incremental AST analysis."""
    def __init__(self, project_path: str, project_id: str, tenant_id: str):
        self.project_path = project_path
        self.project_id   = project_id
        self.tenant_id    = tenant_id
        self.analyzer     = ASTAnalyzer()

    def on_modified(self, event):
        if event.is_directory:
            return
        # Filter for source files
        if event.src_path.lower().endswith(('.py', '.ts', '.js', '.php', '.go')):
            print(f"[file-watcher] Change detected: {event.src_path}")
            self.analyzer.reanalyze_file(
                self.project_path, event.src_path, self.project_id, self.tenant_id
            )

def start_watcher(project_path: str, project_id: str = "default", tenant_id: str = "local"):
    """Stats the watchdog observer."""
    event_handler = SincFileHandler(project_path, project_id, tenant_id)
    observer = Observer()
    observer.schedule(event_handler, project_path, recursive=True)
    observer.start()
    print(f"[file-watcher] Watching {project_path} for changes...")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()

if __name__ == "__main__":
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else "."
    start_watcher(os.path.abspath(path))
