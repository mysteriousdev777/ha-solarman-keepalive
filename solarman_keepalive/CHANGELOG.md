# Changelog

## 0.1.0

First version.

- Answers the logger's frames (handshake, data, Wi-Fi, heartbeat, report) with the
  cloud's time acknowledgement; never sends requests or Modbus commands of its own. The
  logger sets the inverter clock from the time in the answers.
- Local or UTC time in the replies; refuses to start in the local mode without a known
  time zone.
- `allowed_ips` (each rejected address logged once, the rest counted), idle timeout for
  connections a rebooted logger leaves open, a limit on frame size, up to 8 logger
  connections (the longest-silent one makes room for a new one).
- No answers while the system clock is clearly not set, so as not to set the inverter
  clock wrong.
- Intervals measured with a monotonic clock: a jump of the system clock does not distort
  the counters.
- Status JSON (`/status`) for Home Assistant REST sensors and `/health` for the watchdog,
  on the internal network only; up to 16 status connections, 5 s for a whole request.
- Event log with an hourly summary; frame log (rotated) for troubleshooting and
  `tools/stats.py`.
- Runs on amd64 and aarch64.
- One logger per app: frames from a second logger get replies and a warning.
