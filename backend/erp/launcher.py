from __future__ import annotations

import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

from erp.core.config import get_settings
from erp.core.access import access_key

def main() -> None:
    settings = get_settings()
    if settings.runtime_mode == "worker":
        from erp.remote_worker import run
        run()
        return
    if settings.runtime_mode == "server":
        access_key()  # Fail closed before exposing a server without credentials.
        import uvicorn
        uvicorn.run("erp.main:app", host=settings.host, port=settings.port)
        return
    login_url = f"http://{settings.host}:{settings.port}/#access_token={access_key()}"
    try:
        import json
        with urllib.request.urlopen(f"http://{settings.host}:{settings.port}/health", timeout=1) as response:
            if json.load(response).get("application") == "russia-crossborder-erp":
                webbrowser.open(login_url)
                return
    except Exception:
        pass
    root = Path(__file__).resolve().parents[2]
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    log_dir = settings.data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    with (log_dir / "worker.log").open("a", encoding="utf-8") as worker_log, (log_dir / "server.log").open("a", encoding="utf-8") as server_log:
        from erp.services.jobs import LOCAL_WORKER_QUEUES
        queue_names = LOCAL_WORKER_QUEUES
        workers = [
            subprocess.Popen(
                [sys.executable, "-m", "erp.worker", "--queue", queue_name],
                cwd=root, stdout=worker_log, stderr=subprocess.STDOUT, creationflags=flags,
            )
            for queue_name in queue_names
        ]
        server = subprocess.Popen([sys.executable, "-m", "uvicorn", "erp.main:app", "--host", settings.host, "--port", str(settings.port)], cwd=root, stdout=server_log, stderr=subprocess.STDOUT, creationflags=flags)
        url = f"http://{settings.host}:{settings.port}"
        try:
            for _ in range(60):
                if server.poll() is not None: raise RuntimeError("ERP 服务启动失败，请查看 data/logs/server.log")
                try:
                    urllib.request.urlopen(url + "/health", timeout=1).read(); break
                except Exception: time.sleep(0.5)
            else: raise RuntimeError("ERP 服务启动超时")
            webbrowser.open(login_url)
            print(f"俄罗斯跨境 ERP 已启动：{url}\n关闭此窗口可停止本次服务。")
            while server.poll() is None and all(worker.poll() is None for worker in workers): time.sleep(1)
        finally:
            for process in (server, *workers):
                if process.poll() is None: process.terminate()

if __name__ == "__main__": main()
