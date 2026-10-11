# Onionbread

A Python + Tkinter front end for managing a local Tor CLI process and configuring bridge transports.

## Features

- Connect / disconnect controls and live Tor stdout log
- Tor executable path picker
- Direct Tor, obfs4, Snowflake, and WebTunnel modes
- Pluggable-transport helper path picker, with discovery of common helper names on PATH and beside the Tor executable
- Bridge-line validation against the selected transport
- Temporary Tor configuration and data directory, cleaned up when Tor stops

## Requirements

- Python 3.10+
- Tkinter (usually included with Python; on Debian/Ubuntu install `python3-tk`)
- Tor installed separately and available on PATH, or selected with **Browse…**
- For obfs4, Snowflake, or WebTunnel: a compatible transport helper. The Tor Expert Bundle's `lyrebird` supports these transports; Linux packages may provide `obfs4proxy` or a standalone Snowflake client.
- Bridge lines for the transport you select. Obtain them from the Tor Project's official bridge distribution channels.

## Run

```sh
python onionbread.py
```

The app launches Tor with its own temporary `torrc` and data directory. It exposes a SOCKS5 proxy at `127.0.0.1:9050`; it does not automatically configure browsers or other applications to use it.

## Using a bridge

1. Select **obfs4**, **Snowflake**, or **WebTunnel**.
2. Select the matching transport helper executable. With the Tor Expert Bundle, this is commonly `lyrebird.exe` on Windows or `lyrebird` on Linux. The app attempts to find common names automatically, but the picker is available if it cannot.
3. Paste complete bridge lines for the selected transport, one per line. The app adds the `Bridge` prefix if you omit it and checks that each line's transport matches your selection.
4. Press **Connect** and inspect the Tor log for configuration errors and bootstrap progress.

The app writes a matching `ClientTransportPlugin` and `UseBridges 1` entry into its temporary configuration. It does not download bridge lines, install Tor or transport binaries, or guarantee that a bridge is online. Snowflake bridge parameters can change; use a current bridge line from the Tor Project rather than relying on an old copied example.

## Scope / limitations

This is an early CLI wrapper, not a full Orbot equivalent. It does not configure a system-wide VPN, transparent proxying, or per-app routing. It also does not yet show a dedicated bootstrap percentage/status indicator; use the log to see progress. Avoid running another Tor instance already bound to port 9050 at the same time.
