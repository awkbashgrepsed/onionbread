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
TRANSPORTS = ("Direct Tor (no bridges)", "obfs4", "Snowflake", "WebTunnel")


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
        self.transport_path = tk.StringVar(value="")
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
        ttk.Label(bridge, text="Transport helper").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.transport_entry = ttk.Entry(bridge, textvariable=self.transport_path)
        self.transport_entry.grid(row=1, column=1, sticky="ew", padx=(8, 0), pady=(8, 0))
        self.transport_browse = ttk.Button(bridge, text="Browse…", command=self._browse_transport)
        self.transport_browse.grid(row=1, column=2, padx=(6, 0), pady=(8, 0))
        ttk.Label(bridge, text="Bridge line(s)").grid(row=2, column=0, sticky="nw", pady=(8, 0))
        self.bridge_entry = ScrolledText(bridge, height=3, wrap="word")
        self.bridge_entry.grid(row=2, column=1, columnspan=2, sticky="ew", padx=(8, 0), pady=(8, 0))
        self.bridge_hint = ttk.Label(
            bridge, text="Paste bridge lines supplied by Tor Project, one per line.",
            wraplength=450
        )
        self.bridge_hint.grid(row=3, column=1, columnspan=2, sticky="w", padx=(8, 0), pady=(4, 0))
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

    def _browse_transport(self) -> None:
        path = filedialog.askopenfilename(title="Select pluggable transport helper")
        if path:
            self.transport_path.set(path)

    def _update_bridge_controls(self) -> None:
        direct = self.transport.get() == TRANSPORTS[0]
        state = "disabled" if direct else "normal"
        self.bridge_entry.configure(state=state)
        self.transport_entry.configure(state=state)
        self.transport_browse.configure(state=state)
        if direct:
            self.bridge_hint.configure(text="Connect without bridges. Your network must allow direct Tor connections.")
        elif self.transport.get() == "Snowflake":
            self.bridge_hint.configure(text="Paste a current Snowflake Bridge line. The helper can be Tor Expert Bundle's lyrebird executable.")
        elif self.transport.get() == "obfs4":
            self.bridge_hint.configure(text="Paste complete obfs4 Bridge lines. The helper can be Tor Expert Bundle's lyrebird or an obfs4proxy executable.")
        else:
            self.bridge_hint.configure(text="Paste complete WebTunnel Bridge lines. The helper can be Tor Expert Bundle's lyrebird executable.")

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

    def _find_transport_helper(self) -> str:
        selected = self.transport_path.get().strip()
        if selected:
            if Path(selected).is_file():
                return selected
            found = shutil.which(selected)
            if found:
                return found
            raise ValueError(f"Cannot find transport helper: {selected}")

        names = {
            "obfs4": ("lyrebird", "lyrebird.exe", "obfs4proxy", "obfs4proxy.exe"),
            "Snowflake": ("lyrebird", "lyrebird.exe", "snowflake-client", "snowflake-client.exe"),
            "WebTunnel": ("lyrebird", "lyrebird.exe"),
        }.get(self.transport.get(), ())
        for name in names:
            found = shutil.which(name)
            if found:
                return found

        tor = Path(self.tor_path.get().strip())
        roots = [tor.parent, tor.parent / "pluggable_transports"]
        for root in roots:
            for name in names:
                candidate = root / name
                if candidate.is_file():
                    return str(candidate)
        raise ValueError(
            "Transport helper not found. Select lyrebird (from the Tor Expert Bundle) "
            "or the matching transport executable."
        )

    def _make_torrc(self) -> str:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="onionbread-")
        data_dir = Path(self.temp_dir.name) / "data"
        data_dir.mkdir()
        lines = [
            f'DataDirectory {str(data_dir)}',
            "SocksPort 127.0.0.1:9050",
            "SocksPolicy accept 127.0.0.1",
            "SocksPolicy reject *",
            "Log notice stdout",
            "SafeLogging 1",
            "AvoidDiskWrites 1",
        ]
        mode = self.transport.get()
        if mode != TRANSPORTS[0]:
            bridges = [line.strip() for line in self.bridge_entry.get("1.0", "end").splitlines() if line.strip()]
            if not bridges:
                raise ValueError(f"Paste at least one {mode} bridge line.")

            normalized = []
            for bridge in bridges:
                if not bridge.lower().startswith("bridge "):
                    bridge = "Bridge " + bridge
                parts = bridge.split()
                if len(parts) < 2 or parts[0].lower() != "bridge" or parts[1].lower() != mode.lower():
                    raise ValueError(f"Bridge line does not match selected transport ({mode}): {bridge}")
                normalized.append(bridge)

            helper = self._find_transport_helper()
            # Tor's managed-proxy parser treats quotes after `exec` as part
            # of the executable name on Windows. Keep the path unquoted; the
            # normal Tor Expert Bundle path contains no spaces.
            helper_arg = helper.replace("\\", "/")
            if any(ch.isspace() for ch in helper_arg):
                raise ValueError(
                    "The transport helper path contains spaces. Move lyrebird.exe "
                    "to a path without spaces, then select it again."
                )
            lines.extend([
                "UseBridges 1",
                f"ClientTransportPlugin {mode.lower()} exec {helper_arg}",
            ])
            lines.extend(normalized)
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
