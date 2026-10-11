#!/usr/bin/env python3
"""Onionbread: a small Tk GUI for managing a local Tor CLI process.

Requires Tor (and any pluggable transports you select) to be installed separately.
This app does not bundle Tor or guarantee that a selected transport is available.
"""
from __future__ import annotations

import os
import queue
import shutil
import subprocess
import tempfile
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText


APP_NAME = "Onionbread"
TRANSPORTS = ("No bridges (direct Tor)", "Custom bridge lines")


class OnionbreadApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(APP_NAME + " — Tor control")
        self.root.geometry("650x500")
        self.root.minsize(540, 420)

        self.process: subprocess.Popen[str] | None = None
        self.reader_thread: threading.Thread | None = None
        self.messages: queue.Queue[tuple[str, str]] = queue.Queue()
        self.temp_dir: tempfile.TemporaryDirectory[str] | None = None
        self.tor_path = tk.StringVar(value=shutil.which("tor") or "")
        self.transport = tk.StringVar(value=TRANSPORTS[0])
        self.status = tk.StringVar(value="Disconnected")
        self._closing = False

        self._build_ui()
        self._update_bridge_controls()
        self.root.after(100, self._drain_messages)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill="both", expand=True)

        header = ttk.Frame(outer)
        header.pack(fill="x")
        ttk.Label(header, text="ONIONBREAD", font=("TkDefaultFont", 14, "bold")).pack(side="left")
        ttk.Label(header, textvariable=self.status).pack(side="right")

        binary = ttk.LabelFrame(outer, text="Tor executable", padding=8)
        binary.pack(fill="x", pady=(12, 8))
        ttk.Entry(binary, textvariable=self.tor_path).pack(side="left", fill="x", expand=True)
        ttk.Button(binary, text="Browse…", command=self._browse_tor).pack(side="left", padx=(6, 0))

        bridge = ttk.LabelFrame(outer, text="Bridge configuration", padding=8)
        bridge.pack(fill="x", pady=(0, 8))
        ttk.Label(bridge, text="Transport").grid(row=0, column=0, sticky="w")
        self.transport_box = ttk.Combobox(
            bridge, textvariable=self.transport, values=TRANSPORTS, state="readonly"
        )
        self.transport_box.grid(row=0, column=1, sticky="ew", padx=(8, 0))
        self.transport_box.bind("<<ComboboxSelected>>", lambda _event: self._update_bridge_controls())
        ttk.Label(bridge, text="Bridge line(s)").grid(row=1, column=0, sticky="nw", pady=(8, 0))
        self.bridge_entry = ScrolledText(bridge, height=3, wrap="word")
        self.bridge_entry.grid(row=1, column=1, sticky="ew", padx=(8, 0), pady=(8, 0))
        self.bridge_hint = ttk.Label(
            bridge, text="Paste bridge lines supplied by Tor Project, one per line.",
            wraplength=450
        )
        self.bridge_hint.grid(row=2, column=1, sticky="w", padx=(8, 0), pady=(4, 0))
        bridge.columnconfigure(1, weight=1)

        controls = ttk.Frame(outer)
        controls.pack(fill="x", pady=(0, 8))
        self.connect_button = ttk.Button(controls, text="Connect", command=self.connect)
        self.connect_button.pack(side="left")
        self.disconnect_button = ttk.Button(
            controls, text="Disconnect", command=self.disconnect, state="disabled"
        )
        self.disconnect_button.pack(side="left", padx=(8, 0))
        ttk.Button(controls, text="Clear log", command=self._clear_log).pack(side="right")

        log_frame = ttk.LabelFrame(outer, text="Tor log", padding=6)
        log_frame.pack(fill="both", expand=True)
        self.log = ScrolledText(log_frame, height=12, wrap="word", state="disabled")
        self.log.pack(fill="both", expand=True)
        self._append_log("Ready. Select your Tor executable, then connect.")
        self._append_log("Bridge lines do not install pluggable transports; configure those separately if your bridges require them.")

    def _browse_tor(self) -> None:
        path = filedialog.askopenfilename(title="Select Tor executable")
        if path:
            self.tor_path.set(path)

    def _update_bridge_controls(self) -> None:
        custom = self.transport.get() == "Custom bridge lines"
        state = "normal" if custom else "disabled"
        self.bridge_entry.configure(state=state)
        if self.transport.get() == "No bridges (direct Tor)":
            self.bridge_hint.configure(text="Connect without bridges. Your network must allow direct Tor connections.")
        elif custom:
            self.bridge_hint.configure(text="Paste complete bridge lines from an official or trusted source, one per line. Pluggable transports must be installed separately.")
        else:
            self.bridge_hint.configure(text=(
                "This selects the bridge transport type; it does not download bridges or install a transport. "
                "Your Tor package must support it and may need a matching ClientTransportPlugin entry."
            ))

    def _append_log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text.rstrip() + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _clear_log(self) -> None:
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def _set_connected(self, connected: bool) -> None:
        self.status.set("Running" if connected else "Disconnected")
        self.connect_button.configure(state="disabled" if connected else "normal")
        self.disconnect_button.configure(state="normal" if connected else "disabled")

    def _make_torrc(self) -> str:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="onionbread-")
        data_dir = Path(self.temp_dir.name) / "data"
        data_dir.mkdir()
        lines = [
            f'DataDirectory "{data_dir.as_posix()}"',
            "SocksPort 127.0.0.1:9050",
            "SocksPolicy accept 127.0.0.1",
            "SocksPolicy reject *",
            "Log notice stdout",
            "SafeLogging 1",
            "AvoidDiskWrites 1",
        ]
        mode = self.transport.get()
        if mode != "No bridges (direct Tor)":
            lines.append("UseBridges 1")
            if mode == "Custom bridge lines":
                bridges = [line.strip() for line in self.bridge_entry.get("1.0", "end").splitlines() if line.strip()]
                if not bridges:
                    raise ValueError("Enter at least one bridge line.")
                for line in bridges:
                    if not line.lower().startswith("bridge "):
                        line = "Bridge " + line
                    lines.append(line)
            else:
                # Tor's bridge type is not itself a complete bridge address. This
                # setting helps Tor use bridges of the selected transport when
                # suitable Bridge lines are also supplied in the torrc.
                raise ValueError(
                    "A transport type alone is not enough to connect. choose Custom bridge lines and paste "
                    "bridge lines, or add transport-specific setup for your installed Tor package."
                )
        return "\n".join(lines) + "\n"

    def connect(self) -> None:
        if self.process and self.process.poll() is None:
            return
        binary = self.tor_path.get().strip()
        if not binary:
            messagebox.showerror("Tor not found", "Choose the Tor executable or install Tor and make sure 'tor' is on PATH.")
            return
        if not Path(binary).is_file() and shutil.which(binary) is None:
            messagebox.showerror("Tor not found", f"Cannot find executable: {binary}")
            return
        try:
            torrc = self._make_torrc()
        except ValueError as exc:
            messagebox.showwarning("Bridge configuration", str(exc))
            if self.temp_dir:
                self.temp_dir.cleanup()
                self.temp_dir = None
            return
        config_path = Path(self.temp_dir.name) / "torrc"
        config_path.write_text(torrc, encoding="utf-8")
        command = [binary, "-f", str(config_path)]
        self._append_log("$ " + " ".join(command))
        try:
            self.process = subprocess.Popen(
                command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1
            )
        except OSError as exc:
            self._append_log(f"Failed to start Tor: {exc}")
            self._cleanup_temp()
            return
        self._set_connected(True)
        self.reader_thread = threading.Thread(target=self._read_output, args=(self.process,), daemon=True)
        self.reader_thread.start()

    def _read_output(self, process: subprocess.Popen[str]) -> None:
        if process.stdout is not None:
            for line in process.stdout:
                self.messages.put(("log", line.rstrip()))
        code = process.wait()
        self.messages.put(("exit", str(code)))

    def _drain_messages(self) -> None:
        try:
            while True:
                kind, value = self.messages.get_nowait()
                if kind == "log":
                    self._append_log(value)
                elif kind == "exit":
                    self._append_log(f"Tor exited with code {value}.")
                    self.process = None
                    self._set_connected(False)
                    self._cleanup_temp()
        except queue.Empty:
            pass
        if not self._closing:
            self.root.after(100, self._drain_messages)

    def disconnect(self) -> None:
        process = self.process
        if not process:
            return
        self._append_log("Stopping Tor…")
        try:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        except OSError as exc:
            self._append_log(f"Could not stop Tor cleanly: {exc}")
        self.process = None
        self._set_connected(False)
        self._cleanup_temp()

    def _cleanup_temp(self) -> None:
        if self.temp_dir:
            self.temp_dir.cleanup()
            self.temp_dir = None

    def _on_close(self) -> None:
        self._closing = True
        if self.process and self.process.poll() is None:
            self.disconnect()
        self._cleanup_temp()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    OnionbreadApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
