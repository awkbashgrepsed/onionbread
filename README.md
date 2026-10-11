# Onionbread

A small Python + Tkinter front end for starting and stopping a local Tor CLI process.

## Current first step

- Connect / disconnect buttons
- Live Tor stdout log with a clear button
- Tor executable path picker
- Bridge mode selector: direct connection or custom bridge lines
- Temporary Tor configuration and data directory, cleaned up when Tor stops

## Requirements

- Python 3.10+
- Tkinter (usually included with Python; on Debian/Ubuntu install `python3-tk`)
- Tor installed separately and available on PATH, or selected with **Browse…**
- For bridges that use a pluggable transport, that transport must also be installed and configured for your Tor distribution

## Run

```sh
python onionbread.py
```

The app launches Tor with its own temporary torrc and uses SOCKS5 at `127.0.0.1:9050`. It does not change the system Tor configuration or configure applications to use the SOCKS proxy automatically.

## Bridges

Choose **Custom bridge lines** and paste one or more `Bridge` lines from a source you trust. The app does not ship hard-coded bridge addresses because they can become stale and may be unsuitable for a user's network. It also does not fetch bridges from Tor Project or install transports. For example, an obfs4 bridge requires a compatible obfs4 transport binary and the corresponding `ClientTransportPlugin` configuration; providing a bridge line alone does not install that dependency.

## Scope / limitations

This is an initial CLI wrapper, not yet a full Orbot-equivalent. It currently manages the Tor process and log only. It does not configure a system-wide VPN, transparent proxying, or per-app routing. Tor may start successfully before it has finished bootstrapping; use the log to see bootstrap progress. Avoid running another Tor instance already bound to port 9050 at the same time.
