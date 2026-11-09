# Solarman Keepalive

[![CI](https://github.com/mysteriousdev777/ha-solarman-keepalive/actions/workflows/ci.yml/badge.svg)](https://github.com/mysteriousdev777/ha-solarman-keepalive/actions/workflows/ci.yml)

A Home Assistant app that stands in for the Solarman cloud on your local network, so that a
Solarman Wi-Fi logger without internet access stays connected instead of restarting every
30 minutes.

## Why

Solarman Wi-Fi loggers (LSW-3 and similar sticks, used by Deye and other inverters) upload
to the Solarman cloud. Many people block that for privacy and poll the logger locally on
port 8899 instead. But a logger that cannot reach its cloud drops off Wi-Fi and restarts
every 30 minutes, like clockwork, and the local port is gone for 30-45 seconds each time:
failed polls, `unavailable` sensors, commands that do not go through.

With this app as its server, the logger keeps one connection open and stops restarting,
apart from its built-in restart every 3 hours (back within about 35 seconds).

## What it does, and what it does not

- Only answers the logger's frames, with the acknowledgement the cloud sends, and never
  sends requests or Modbus commands of its own.
- Each answer carries the current time, and the logger sets the inverter clock from it:
  the one thing the app changes, as the Solarman cloud does. The time is local by default,
  as with the cloud; without a known time zone the app refuses to start rather than shift
  the clock.
- Keeps nothing and forwards nothing. An optional frame log stays on your machine.
- Reports the logger's state as JSON for Home Assistant REST sensors.
- One logger per app: a second logger also gets replies, but the status follows one logger,
  so the app warns about it.

Tested with a **Deye SUN-5K-SG03LP1-EU** (single-phase hybrid inverter, 5 kW) and its
LSW-3 logger, firmware `LSW3_32U_5406_1.06`. Without a server, this logger restarted exactly
every 30 minutes. Other loggers that speak Solarman V5 are likely to behave the same way,
but were not tested. The app runs on amd64 and aarch64 Home Assistant hosts.

## Open question: the restart every 3 hours

Even with this app, the logger restarts every 3 hours, and its local port is gone for about
35 seconds each time. It looks like a Solarman default, and no way to turn it off is known:
neither the logger's web settings nor the AT commands documented for its Wi-Fi module have
one. If you know a way to turn it off, sharing it in an issue would be great.

## Install

1. In Home Assistant, open the app store, choose **Repositories** in the top-right menu and
   add `https://github.com/mysteriousdev777/ha-solarman-keepalive`.
2. Install **Solarman Keepalive**, start it, and follow its **Documentation** tab: set
   `allowed_ips`, then point the logger's Server A and Optional Server at Home Assistant,
   port 10000.

The app is built on your device from this repository; there is no prebuilt image. Updates
are not installed automatically unless you turn that on.

## How it works

The logger talks Solarman V5 to its server: frames of the form

```
A5 | length (u16 LE) | control | type | sequence (2 bytes) | logger serial (u32 LE) | payload | checksum | 15
```

with types 0x41 handshake, 0x42 data, 0x43 Wi-Fi, 0x47 heartbeat and 0x48 report. The
server answers each with a 23-byte frame of type `request - 0x30`, the first sequence byte
plus one, and a payload holding the current time. The reply is the one of
[dummycloud](https://github.com/Hypfer/deye-microinverter-cloud-free) by Hypfer; the frame
layout follows the [pysolarmanv5](https://pysolarmanv5.readthedocs.io/) documentation.

Things learned on the way, useful when you read the logs:

- Without a server the logger restarts every 30 minutes. With a server that accepts the
  connection but never replies, it repeats its login every 30 seconds and restarted after
  90 minutes (seen once). With replies it logs in once per boot and sends current data
  about every 5.5 minutes.
- It stores data it could not send and uploads it after the next login.
- It restarts every 3 hours whatever happens (see the open question above).
- While it answers a local Modbus client on port 8899 it misses replies that arrive at the
  same moment, and sends the data again about 34 seconds later. A reply repeated 5 seconds
  later is not accepted either, and the sequence bytes do not matter: it simply waits for
  the reply for only a few seconds. The app counts these as `not_accepted`.

## Standalone

The server is one Python file with no dependencies (Python 3.11 or newer):

```
python solarman_keepalive/keepalive.py --timezone Europe/Berlin --allow <logger address>
```

`--help` lists the options. On Windows without the `tzdata` package use
`--timezone system` (the time zone of the operating system).

## Development

```
python -m unittest discover -s tests       # tests, standard library only
python tools/stats.py frames.log           # metrics of a frame log
python tools/privacy_check.py --history    # before every push, see below
```

CI runs the tests on Python 3.11 and 3.14, checks the app definition
(`tools/check_config.py`), and builds the image for both architectures (amd64, aarch64) on
native runners and runs it against a fake logger.

**Privacy.** Frame logs contain the logger's serial number and addresses, so `*.log` is
ignored. `tools/privacy_check.py` fails on private addresses, MAC addresses and serial
numbers in the files; list your own serial numbers, network names and e-mail addresses in
`.privacy-local` (ignored by git) and run it with `--history` before pushing.

**Base image.** The Dockerfile pins `ghcr.io/home-assistant/base-python` to a dated tag.
Change it on purpose, with a CHANGELOG entry and a new version in `config.yaml` and
`keepalive.py`.

## License

[MIT](LICENSE). Not affiliated with Solarman, IGEN Tech or any inverter maker.
