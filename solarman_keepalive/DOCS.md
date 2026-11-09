# Solarman Keepalive

## What it does

A Solarman Wi-Fi logger (LSW-3 and similar sticks on Deye and other inverters) sends its
data to the Solarman cloud. When it cannot reach the cloud, because its internet access is
blocked or the cloud is gone, it does not just stop uploading: it drops off Wi-Fi and
restarts every 30 minutes. Each time, the local Modbus port that Home Assistant
integrations poll (8899) is unavailable for 30-45 seconds.

This app answers the logger the way the cloud does. The logger then keeps one connection
open and stops restarting, except for its built-in restart every 3 hours.

- **Answers only.** The app only answers the logger's frames, with the cloud's
  acknowledgement, and never sends requests or Modbus commands of its own.
- **The logger sets the inverter clock** from the time in the answers: the one thing the
  app changes, as the Solarman cloud does. So the time zone matters (see `time`).
- **Nothing leaves your network**, and the logger's data is not stored (unless you turn on
  the frame log).

## Setup

1. Install and start the app. In the **Log** tab, check the line
   `time in replies: local, <your time zone> (UTC+..)`.
2. Recommended: set `allowed_ips` to your logger's address. In the **Configuration** tab,
   add the address as an item of the list, or in YAML mode:

   ```yaml
   allowed_ips:
     - 192.0.2.10
   ```

   (your logger's address instead of `192.0.2.10`; it is on the logger's status page and in
   your router). If the log then says `rejected a connection from an address not in
   allowed_ips` once the logger is pointed here, set `log_level: debug` with an empty list:
   the `open` lines show the address the app sees.
3. Point the logger at Home Assistant. Open `http://<logger address>/config_hide.html` (the
   logger's web interface) and set **Server A Setting** and **Optional Server Setting** to the
   address of Home Assistant, port `10000`, TCP. Saving restarts the logger. Write down the
   old values first, to be able to go back.
4. Within a minute the log shows `logger logged in`.

## Options

| Option | Default | Meaning |
|---|---|---|
| `time` | `local` | Time in the replies. `local`: the local time of the Home Assistant time zone, as the Solarman cloud sends. `utc`: UTC. |
| `timezone` | unset | Time zone for `local`, for example `Europe/Berlin`. Unset: the Home Assistant time zone. Without a known time zone the app does not start, rather than shift the inverter clock. |
| `allowed_ips` | empty | Accept connections only from these addresses: put your logger's address here. Empty: any address. |
| `idle_timeout` | `15` | Minutes without data before the app closes a connection. A logger that restarts leaves its old connection open; the logger sends data every 5-6 minutes. `0`: never. |
| `log_level` | `info` | `info`: events and an hourly summary. `debug`: also every frame, with the logger's serial number and address. |
| `frame_log` | `false` | Write every frame to `frames.log` in the app's configuration folder (rotated, up to 4 x 5 MB). |

## Status in Home Assistant

The app serves `GET /status` (JSON) and `GET /health` on port 8080 of the internal network.
Home Assistant reaches it by the app's host name: the app ID with `_` replaced by `-`, for
example `1a2b3c4d-solarman-keepalive` (the app ID is in the address bar of the app's page).
The port does not need to be opened on the host.

```yaml
rest:
  - resource: http://1a2b3c4d-solarman-keepalive:8080/status
    scan_interval: 60
    binary_sensor:
      - name: Solarman Logger Connected
        device_class: connectivity
        value_template: "{{ value_json.connected }}"
    sensor:
      - name: Solarman Logger Last Frame
        device_class: timestamp
        value_template: "{{ value_json.last_frame }}"
      - name: Solarman Logger Uptime
        device_class: duration
        unit_of_measurement: s
        value_template: "{{ value_json.logger_uptime_s }}"
      - name: Solarman Logger Replies Not Accepted
        unit_of_measurement: "%"
        value_template: "{{ value_json.last_hour.not_accepted_pct }}"
```

| Field | Meaning |
|---|---|
| `connected` | An open connection carries frames, and the last one is recent. |
| `loggers` | How many different loggers sent frames: 1, or the status mixes them up. |
| `last_frame`, `last_frame_age_s` | Time of the last frame (UTC) and its age in seconds. |
| `last_login` | Last login (handshake) of the logger. |
| `logger_boot`, `logger_uptime_s` | When the logger last started, from the uptime it reports. |
| `clock_ok` | The system clock looks set. While it does not, the app does not answer (see below). |
| `last_hour` | Data frames, replies not accepted (count and %), reconnects, logins, logger restarts and rejected connections in the last hour. |
| `totals` | The same and more since the app started: frames left unanswered while the clock was not set (`held`), connections closed to make room (`evicted`). |

The status contains no serial numbers and no addresses.

## Logs

- **Log tab** (`info`): logins, reconnects, logger restarts, replies the logger did not
  accept, connections closed, and a summary every hour. No serial numbers, addresses or
  network names.
- **`debug`**: every frame in the Log tab as well.
- **Frame log** (`frame_log: true`): every frame in `frames.log`, in the app's configuration
  folder (`app_configs/<app ID>/`; `addon_configs` in older Home Assistant versions).
  `tools/stats.py` in the repository turns it into metrics. It contains the logger's serial
  number and address: do not publish it.

## Good to know

- **One logger per app.** The status and the log follow a single logger. A second logger
  pointed here also gets replies and stays online, but the app warns
  (`frames from 2 different loggers`), and its status then mixes them up.
- **The restart every 3 hours is built into the logger** and stays. The logger is back
  within about 35 seconds.
- **Some replies are not accepted** while a local client polls the logger on port 8899 (a
  Modbus integration in Home Assistant, for example): the logger then reads its data again
  and sends it about 34 seconds later, or reconnects. This is harmless; the status shows
  it as `not_accepted`. The more often the logger is polled, the more often it happens.
- **The app does not answer while the system clock is clearly not set** (earlier than the
  release of the app version, as on a host without a battery-backed clock before time
  sync): an answer would set the inverter clock wrong. It logs a warning and answers again
  as soon as the clock is set.
- **Up to 8 logger connections.** When a new one comes and 8 are open, the app closes the
  one silent for the longest time: usually the one a restarted logger left behind.
- **Restarting the app does not restart the logger.** It reconnects within seconds.
- **If the app is stopped for long**, the logger goes back to restarting every 30
  minutes. Nothing breaks.

## Troubleshooting

- `time 'local' needs a time zone`: set the Home Assistant time zone, or the `timezone`
  option.
- No `logger logged in`: check Server A in the logger (the Home Assistant address, port
  10000, TCP), that port 10000 is enabled in the app's Network section, and `allowed_ips`.
- `rejected a connection from an address not in allowed_ips`: another device, or the
  logger got a new address. It is logged once per address; the hourly summary and the
  status count the rest.
- `the system clock says …, before this version was released`: the host's time is not set;
  the app waits for it before answering the logger.
- To go back: restore the logger's original server settings.
