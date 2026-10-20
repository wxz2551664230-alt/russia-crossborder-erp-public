"""Desktop launcher. Business pages live in the existing ERP web application."""
import json
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox
import urllib.request
import webbrowser

from erp_desktop import Client, VERSION, load_profile, login, profile_dir, remembered_login, restore_login, start_local_worker


def worker_status(profile):
    request = urllib.request.Request('http://127.0.0.1:48739/status', headers={'Origin': profile['server']})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=2) as response:
            result = json.load(response)
    except (OSError, ValueError):
        return False
    return bool(result.get('ready') and result.get('worker_id') and result.get('user_id') == profile['user']['id'])


def launch():
    root = tk.Tk()
    root.title(f'俄罗斯跨境 ERP · 桌面连接端 {VERSION}')
    root.geometry('760x510'); root.minsize(680, 480)
    root.option_add('*Font', ('Microsoft YaHei UI', 10))
    outer = ttk.Frame(root, padding=24); outer.pack(fill='both', expand=True)
    ttk.Label(outer, text='俄罗斯跨境 ERP', font=('Microsoft YaHei UI', 20, 'bold')).pack(anchor='w')
    ttk.Label(outer, text='完整业务在 ERP 网页中操作；任务在当前电脑执行，资料与结果同步云端。', wraplength=700).pack(anchor='w', pady=(8, 20))
    messages = queue.Queue(); buttons = []; active = False
    status = tk.StringVar(value='请登录并连接当前电脑')
    server = tk.StringVar(value='https://erp.example.com'); username = tk.StringVar(); password = tk.StringVar()
    remember = tk.BooleanVar(value=False)
    login_frame = ttk.LabelFrame(outer, text='登录 ERP', padding=16); login_frame.pack(fill='x')
    for index, (label, variable, secret) in enumerate([('服务器地址', server, False), ('账号', username, False), ('密码', password, True)]):
        ttk.Label(login_frame, text=label).grid(row=index, column=0, sticky='w', pady=6)
        ttk.Entry(login_frame, textvariable=variable, show='•' if secret else '', width=50).grid(row=index, column=1, sticky='ew', padx=12)
    login_frame.columnconfigure(1, weight=1)
    ttk.Checkbutton(login_frame, text='记住密码并自动登录（仅这台电脑）', variable=remember).grid(row=3, column=1, sticky='w', pady=8)
    workspace = ttk.LabelFrame(outer, text='当前连接', padding=18)
    account = tk.StringVar(); address = tk.StringVar(); execution = tk.StringVar(value='正在连接本机执行端…')
    for label, variable in [('登录账号', account), ('云端服务', address), ('本机执行端', execution)]:
        row = ttk.Frame(workspace); row.pack(fill='x', pady=6)
        ttk.Label(row, text=label, width=12).pack(side='left')
        ttk.Label(row, textvariable=variable, wraplength=500).pack(side='left')
    ttk.Label(workspace, text='产品库、POD、上架、订单及系统设置请在完整 ERP 页面使用。', wraplength=620).pack(anchor='w', pady=(14, 8))
    toolbar = ttk.Frame(workspace); toolbar.pack(fill='x', pady=8)

    def background(work, callback):
        status.set('正在连接…')
        for button in buttons: button.configure(state='disabled')
        def run():
            try: messages.put((callback, work(), None))
            except Exception as error: messages.put((callback, None, error))
        threading.Thread(target=run, daemon=True).start()

    def open_web():
        webbrowser.open(Client().base + '/catalog/products')
        status.set('已打开完整 ERP；保持本机执行端运行即可接收任务')

    def connected(result):
        execution.set('已连接 · 任务在这台电脑执行' if result else '尚未连接，请确认使用这台电脑已绑定的账号，再重新连接')
        status.set('共享数据保存在云端；关闭此窗口不会停止已启动的执行端')

    def reconnect():
        def work():
            start_local_worker()
            profile = load_profile()
            for attempt in range(4):
                if worker_status(profile): return True
                if attempt < 3: time.sleep(1)
            return False
        background(work, connected)

    def logged_in(user):
        nonlocal active
        active = True; password.set('')
        profile = load_profile()
        account.set(f"{user['display_name']}（{user['username']}）")
        address.set(profile['server'])
        login_frame.pack_forget(); workspace.pack(fill='x')
        open_web(); reconnect()

    def sign_in():
        values = server.get(), username.get(), password.get(); password.set('')
        enabled = remember.get()
        background(lambda: login(*values, remember_password=enabled), logged_in)

    def signed_out(warning):
        nonlocal active
        active = False; workspace.pack_forget(); login_frame.pack(fill='x')
        status.set('本机已退出，请在网页中同时退出原账号' if not warning else '本机登录已清除；服务器会话撤销未确认')

    def sign_out():
        def work():
            warning = False
            try: Client().request('POST', '/auth/logout')
            except Exception: warning = True
            finally:
                (profile_dir() / 'session.dat').unlink(missing_ok=True)
                (profile_dir() / 'remembered-login.dat').unlink(missing_ok=True)
            return warning
        background(work, signed_out)

    for parent, text, command in [(login_frame, '登录并打开 ERP', sign_in), (toolbar, '打开完整 ERP', open_web), (toolbar, '重新连接执行端', reconnect), (toolbar, '退出登录', sign_out)]:
        button = ttk.Button(parent, text=text, command=command)
        if parent is login_frame: button.grid(row=4, column=1, sticky='e', pady=8)
        else: button.pack(side='left', padx=(0, 10))
        buttons.append(button)
    ttk.Label(outer, textvariable=status, wraplength=700).pack(side='bottom', anchor='w', pady=12)

    def poll():
        try:
            while True:
                callback, result, error = messages.get_nowait()
                for button in buttons: button.configure(state='normal')
                if error:
                    status.set(str(error)); messagebox.showerror('连接未完成', str(error), parent=root)
                else: callback(result)
        except queue.Empty: pass
        root.after(100, poll)
    try:
        existing = load_profile(); server.set(existing['server']); username.set(existing['user']['username'])
        remember.set(bool(remembered_login()))
        background(restore_login, logged_in)
    except Exception: pass
    poll(); root.mainloop()


if __name__ == '__main__': launch()
