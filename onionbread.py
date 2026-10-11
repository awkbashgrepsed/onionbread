#!/usr/bin/env python3
"""Onionbread: a small Tk GUI for managing a local Tor CLI process.

Requires Tor (and any pluggable transports you select) to be installed separately.
This app does not bundle Tor or guarantee that a selected transport is available.
"""
from __future__ import annotations

import json
import os
import queue
import re
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
        self.root.geometry("650x600")
        self.root.minsize(540, 520)

        self.process: subprocess.Popen[str] | None = None
        self.reader_thread: threading.Thread | None = None
        self.messages: queue.Queue[tuple[str, str]] = queue.Queue()
        self.temp_dir: tempfile.TemporaryDirectory[str] | None = None
        self.settings_path = self._settings_file()
        saved_settings = self._load_settings()
        self.tor_path = tk.StringVar(
            value=saved_settings.get("tor_path") or self._discover_tor()
        )
        saved_transport = saved_settings.get("transport", TRANSPORTS[0])
        if saved_transport not in TRANSPORTS:
            saved_transport = TRANSPORTS[0]
        self.transport = tk.StringVar(value=saved_transport)
        self.transport_path = tk.StringVar(value=saved_settings.get("transport_path", ""))
        self._saved_bridge_lines = saved_settings.get("bridge_lines", "")
        self._auto_transport_path = ""
        self.status = tk.StringVar(value="Disconnected")
        self.bootstrap_text = tk.StringVar(value="Not connected")
        self.bootstrap_progress = tk.IntVar(value=0)
        self._closing = False

        self._build_ui()
        self._update_bridge_controls()
        self._refresh_transport_helper()
        self.tor_path.trace_add("write", self._on_tor_path_changed)
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
        self.transport_box.bind("<<ComboboxSelected>>", lambda _event: self._on_transport_changed())
        ttk.Label(bridge, text="Transport helper").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.transport_entry = ttk.Entry(bridge, textvariable=self.transport_path)
        self.transport_entry.grid(row=1, column=1, sticky="ew", padx=(8, 0), pady=(8, 0))
        self.transport_browse = ttk.Button(bridge, text="Browse…", command=self._browse_transport)
        self.transport_browse.grid(row=1, column=2, padx=(6, 0), pady=(8, 0))
        ttk.Label(bridge, text="Bridge line(s)").grid(row=2, column=0, sticky="nw", pady=(8, 0))
        self.bridge_entry = ScrolledText(bridge, height=3, wrap="word")
        self.bridge_entry.grid(row=2, column=1, sticky="ew", padx=(8, 0), pady=(8, 0))
        self.bridge_paste = ttk.Button(bridge, text="Paste", command=self._paste_bridges)
        self.bridge_paste.grid(row=2, column=2, sticky="n", padx=(6, 0), pady=(8, 0))
        if self._saved_bridge_lines:
            self.bridge_entry.insert("1.0", self._saved_bridge_lines)
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

        # Pack the progress and port rows before the expandable log. If the
        # expanding log is packed first, Tk can squeeze the footer out of view
        # in smaller windows.
        progress = ttk.Frame(outer)
        progress.pack(fill="x", pady=(0, 6))
        ttk.Label(progress, textvariable=self.bootstrap_text).pack(anchor="w")
        self.progress_bar = ttk.Progressbar(
            progress, orient="horizontal", mode="determinate", maximum=100,
            variable=self.bootstrap_progress
        )
        self.progress_bar.pack(fill="x", expand=True, pady=(4, 0))

        footer = ttk.Frame(outer)
        footer.pack(fill="x", pady=(8, 0))
        ttk.Label(
            footer,
            text="SOCKS5 proxy port: 9050 (127.0.0.1)    |    ControlPort: disabled",
        ).pack(side="left")

        log_frame = ttk.LabelFrame(outer, text="Tor log", padding=6)
        log_frame.pack(fill="both", expand=True)

        self.log = ScrolledText(log_frame, height=12, wrap="word", state="disabled")
        self.log.pack(fill="both", expand=True)

        self._append_log("Ready. Tor and Lyrebird are auto-detected when available.")
        self._append_log("Bridge lines are still entered manually; automatic bridge provisioning is not implemented yet.")

    def _settings_file(self) -> Path:
        """Return a per-user settings path, outside the application/runtime directory."""
        if os.name == "nt":
            base = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
        else:
            base = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
        return base / "Onionbread" / "settings.json"

    def _load_settings(self) -> dict[str, str]:
        """Load saved UI configuration; ignore missing or malformed settings."""
        try:
            data = json.loads(self.settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}
        settings: dict[str, str] = {}
        for key in ("tor_path", "transport", "transport_path", "bridge_lines"):
            value = data.get(key)
            if isinstance(value, str):
                settings[key] = value
        return settings

    def _save_settings(self) -> None:
        """Persist selected Tor/transport paths and bridge lines for the next launch."""
        settings = {
            "tor_path": self.tor_path.get().strip(),
            "transport": self.transport.get(),
            "transport_path": self.transport_path.get().strip(),
            "bridge_lines": self.bridge_entry.get("1.0", "end-1c"),
        }
        try:
            self.settings_path.parent.mkdir(parents=True, exist_ok=True)
            self.settings_path.write_text(
                json.dumps(settings, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            self._append_log(f"Could not save settings to {self.settings_path}: {exc}")

    def _discover_tor(self) -> str:
        """Prefer a packaged runtime, then a real install over package-manager shims."""

        app_dir = Path(__file__).resolve().parent
        candidates = [
            app_dir / "runtime" / "tor" / "tor.exe",
            app_dir / "runtime" / "tor.exe",
            app_dir / "tor.exe",
        ]
        scoop_tor = Path.home() / "scoop" / "apps" / "tor"
        candidates.extend([
            scoop_tor / "current" / "tor" / "tor.exe",
            scoop_tor / "current" / "tor.exe",
        ])
        if scoop_tor.is_dir():
            candidates.extend(scoop_tor.glob("*/tor/tor.exe"))
            candidates.extend(scoop_tor.glob("*/tor.exe"))

        # A PATH lookup can point to Scoop's shim, which may inject its own
        # configuration flags. Prefer the real Tor binary when available.
        found = shutil.which("tor") or shutil.which("tor.exe")
        if found and "scoop" not in str(Path(found).parent).lower():
            candidates.append(Path(found))
        valid = [p for p in candidates if p.is_file()]
        if valid:
            return str(max(valid, key=lambda p: p.stat().st_mtime))
        return found or ""

    def _browse_tor(self) -> None:
        path = filedialog.askopenfilename(title="Select Tor executable")
        if path:
            self.tor_path.set(path)

    def _browse_transport(self) -> None:
        path = filedialog.askopenfilename(title="Select pluggable transport helper")
        if path:
            self.transport_path.set(path)

    def _paste_bridges(self) -> None:
        """Insert clipboard text at the current cursor in the bridge field."""
        try:
            clipboard_text = self.root.clipboard_get()
        except tk.TclError:
            messagebox.showwarning("Clipboard unavailable", "No text is available on the clipboard.", parent=self.root)
            return
        self.bridge_entry.focus_set()
        self.bridge_entry.insert("insert", clipboard_text)

    def _update_bridge_controls(self) -> None:
        direct = self.transport.get() == TRANSPORTS[0]
        state = "disabled" if direct else "normal"
        self.bridge_entry.configure(state=state)
        self.bridge_paste.configure(state=state)
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

    def _resolve_tor_executable(self, selected: str) -> str:
        """Replace a Scoop shim with the underlying tor.exe when discoverable."""
        path = Path(selected)
        if path.is_file() and "scoop" in str(path.parent).lower() and "shims" in str(path.parent).lower():
            real_candidates = []
            scoop_tor = Path.home() / "scoop" / "apps" / "tor"
            real_candidates.extend([
                scoop_tor / "current" / "tor" / "tor.exe",
                scoop_tor / "current" / "tor.exe",
            ])
            if scoop_tor.is_dir():
                real_candidates.extend(scoop_tor.glob("*/tor/tor.exe"))
                real_candidates.extend(scoop_tor.glob("*/tor.exe"))
            real_candidates = [p for p in real_candidates if p.is_file()]
            if real_candidates:
                return str(max(real_candidates, key=lambda p: p.stat().st_mtime))
        return selected

    def _transport_helper_names(self) -> tuple[str, ...]:
        return {
            "obfs4": ("lyrebird", "lyrebird.exe", "obfs4proxy", "obfs4proxy.exe"),
            "Snowflake": ("lyrebird", "lyrebird.exe", "snowflake-client", "snowflake-client.exe"),
            "WebTunnel": ("lyrebird", "lyrebird.exe"),
        }.get(self.transport.get(), ())

    def _discover_transport_helper(self) -> str:
        """Find a helper on PATH or alongside the selected Tor installation."""
        names = self._transport_helper_names()
        for name in names:
            found = shutil.which(name)
            if found:
                return found

        selected_tor = self.tor_path.get().strip()
        if not selected_tor:
            return ""
        tor = Path(self._resolve_tor_executable(selected_tor))
        roots = [
            tor.parent / "pluggable_transports",
            tor.parent,
            tor.parent.parent / "pluggable_transports",
        ]
        # Also check the bundled runtime even if the Tor executable was
        # manually selected from elsewhere.
        app_dir = Path(__file__).resolve().parent
        roots.extend([
            app_dir / "runtime" / "tor" / "pluggable_transports",
            app_dir / "runtime" / "pluggable_transports",
        ])
        seen: set[str] = set()
        for root in roots:
            key = str(root).casefold()
            if key in seen:
                continue
            seen.add(key)
            for name in names:
                candidate = root / name
                if candidate.is_file():
                    return str(candidate)
        return ""

    def _refresh_transport_helper(self) -> None:
        """Populate the helper field when a matching executable is discovered."""
        current = self.transport_path.get().strip()
        # Preserve a path the user explicitly entered; replace only a blank
        # field or the previous automatically detected value.
        if current and current != self._auto_transport_path:
            return
        detected = self._discover_transport_helper()
        self._auto_transport_path = detected
        self.transport_path.set(detected)

    def _on_transport_changed(self) -> None:
        self._update_bridge_controls()
        self._refresh_transport_helper()

    def _on_tor_path_changed(self, *_args: object) -> None:
        self._refresh_transport_helper()

    def _find_transport_helper(self) -> str:
        selected = self.transport_path.get().strip()
        if selected:
            if Path(selected).is_file():
                return selected
            found = shutil.which(selected)
            if found:
                return found
            raise ValueError(f"Cannot find transport helper: {selected}")

        found = self._discover_transport_helper()
        if found:
            return found
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
        self._save_settings()
        binary = self.tor_path.get().strip()
        if not binary:
            messagebox.showerror("Tor not found", "Choose the Tor executable or install Tor and make sure 'tor' is on PATH.")
            return
        binary = self._resolve_tor_executable(binary)
        if not Path(binary).is_file() and shutil.which(binary) is None:
            messagebox.showerror("Tor not found", f"Cannot find executable: {binary}")
            return
        self.tor_path.set(binary)
        self.bootstrap_progress.set(0)
        self.bootstrap_text.set("Starting Tor…")
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
                    match = re.search(r"Bootstrapped (\d+)% \(([^)]+)\):? ?(.*)", value)
                    if match:
                        percent = int(match.group(1))
                        stage = match.group(3).strip() or match.group(2).replace("_", " ")
                        self.bootstrap_progress.set(percent)
                        if percent >= 100:
                            self.bootstrap_text.set("Connected — Tor circuit ready")
                            self.status.set("Connected")
                        else:
                            self.bootstrap_text.set(f"Connecting — {percent}% · {stage}")
                    elif "Bootstrapped " not in value and (
                        "Failed to start process" in value or "failed at launch" in value.lower()
                    ):
                        self.bootstrap_text.set("Transport failed to start")
                elif kind == "exit":
                    self._append_log(f"Tor exited with code {value}.")
                    self.process = None
                    self._set_connected(False)
                    self.bootstrap_text.set("Disconnected")
                    self.bootstrap_progress.set(0)
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
        self.bootstrap_text.set("Disconnecting…")
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
        self.bootstrap_text.set("Disconnected")
        self.bootstrap_progress.set(0)
        self._cleanup_temp()

    def _cleanup_temp(self) -> None:
        if self.temp_dir:
            self.temp_dir.cleanup()
            self.temp_dir = None

    def _on_close(self) -> None:
        self._save_settings()
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
