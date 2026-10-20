"""Loopback folder picker for the existing cloud self-listing form.

Only the ERP origin saved at login may read a selected local path. The picker
never exposes the account cookie and never sends directory contents anywhere.
"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
from urllib.parse import urlsplit


PICKER_PORT = 48739


def choose_directory(title: str, initial_dir: str) -> str:
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    try:
        root.withdraw()
        root.attributes('-topmost', True)
        options = {'parent': root, 'title': title or '选择文件夹', 'mustexist': True}
        initial = Path(initial_dir).expanduser() if initial_dir else None
        if initial and initial.is_dir():
            options['initialdir'] = str(initial)
        selected = filedialog.askdirectory(**options)
        return str(Path(selected).resolve()) if selected else ''
    finally:
        root.destroy()


def make_server(origin: str, *, port: int = PICKER_PORT, picker=choose_directory) -> ThreadingHTTPServer:
    parsed = urlsplit(origin)
    if parsed.scheme not in {'https', 'http'} or not parsed.hostname or parsed.path not in {'', '/'}:
        raise ValueError('本机文件夹选择器的 ERP 来源无效')
    allowed_origin = f'{parsed.scheme}://{parsed.netloc}'
    picking = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            if self.path != '/status' or not self._allowed():
                self._reply(403, {'message': '来源不允许'})
                return
            from erp_desktop import load_profile, profile_dir
            try:
                profile = load_profile()
                if profile.get('server', '').rstrip('/') != allowed_origin:
                    raise ValueError('本机账号来源不一致')
                worker_id = (profile_dir() / 'device-id.txt').read_text(encoding='ascii').strip()
                identity = {'worker_id': worker_id, 'user_id': profile['user']['id']}
            except (OSError, ValueError, KeyError):
                identity = {}
            self._reply(200, {'ready': True, **identity})

        def _allowed(self) -> bool:
            return (self.headers.get('Origin') == allowed_origin
                    and self.headers.get('Host') in {f'127.0.0.1:{self.server.server_port}',
                                                     f'localhost:{self.server.server_port}'})

        def _reply(self, status: int, body: dict):
            packed = json.dumps(body, ensure_ascii=False).encode('utf-8')
            self.send_response(status)
            if self._allowed():
                self.send_header('Access-Control-Allow-Origin', allowed_origin)
                self.send_header('Vary', 'Origin')
                self.send_header('Access-Control-Allow-Private-Network', 'true')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(packed)))
            self.end_headers()
            self.wfile.write(packed)

        def do_OPTIONS(self):
            if self.path not in {'/select-directory', '/status'} or not self._allowed():
                self._reply(403, {'message': '来源不允许'})
                return
            self.send_response(204)
            self.send_header('Access-Control-Allow-Origin', allowed_origin)
            self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS' if self.path == '/status' else 'POST, OPTIONS')
            self.send_header('Access-Control-Allow-Headers', 'Content-Type')
            self.send_header('Access-Control-Allow-Private-Network', 'true')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', '0')
            self.end_headers()

        def do_POST(self):
            if self.path != '/select-directory' or not self._allowed():
                self._reply(403, {'message': '来源不允许'})
                return
            if self.headers.get('Content-Type', '').split(';', 1)[0].strip().lower() != 'application/json':
                self._reply(415, {'message': '请求格式无效'})
                return
            try:
                size = int(self.headers.get('Content-Length', '0'))
            except ValueError:
                size = 0
            if not 0 < size <= 5000:
                self._reply(413, {'message': '请求过大'})
                return
            try:
                value = json.loads(self.rfile.read(size))
                title = value.get('title', '')
                initial_dir = value.get('initial_dir', '')
                if not isinstance(title, str) or not isinstance(initial_dir, str) or len(title) > 200 or len(initial_dir) > 4000:
                    raise ValueError('路径参数无效')
            except (ValueError, AttributeError, UnicodeError):
                self._reply(422, {'message': '路径参数无效'})
                return
            if not picking.acquire(blocking=False):
                self._reply(409, {'message': '已有文件夹选择窗口，请先完成选择'})
                return
            try:
                selected = picker(title, initial_dir)
            except Exception:
                self._reply(500, {'message': '无法打开本机文件夹选择器'})
            else:
                self._reply(200, {'path': selected, 'cancelled': not bool(selected)})
            finally:
                picking.release()

    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.daemon_threads = True
    return server


def start_picker(origin: str) -> ThreadingHTTPServer:
    server = make_server(origin)
    threading.Thread(target=server.serve_forever, daemon=True, name='erp-local-picker').start()
    return server
