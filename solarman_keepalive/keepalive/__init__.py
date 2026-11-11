"""Solarman Keepalive: a local stand-in for the Solarman cloud.

A Solarman Wi-Fi logger (LSW-3 and similar sticks) that cannot reach its cloud keeps
rebooting every 30 minutes, and its local Modbus port (8899) goes away each time.
Pointed at this server (logger web UI, config_hide.html: Server A and the Optional
Server -> this host, port 10000, TCP) it gets the replies it expects from the cloud and
stays online; only its built-in reboot every 3 hours remains.

The server only answers the logger's frames with the acknowledgement the cloud sends
(the reply of Hypfer's dummycloud), and never sends requests or Modbus commands of its
own. Each answer carries the current time, and the logger sets the inverter clock from
it: the one thing the server changes, as the Solarman cloud does. So the 'local' mode
sends the local wall-clock time, like the cloud.

Home Assistant app: python -m keepalive --options /data/options.json
Standalone, from the solarman_keepalive folder:
             python -m keepalive --timezone Europe/Kyiv --allow <logger-ip>
             (Windows without the tzdata package: --timezone system)

Modules:
  protocol   Solarman V5 frames and the cloud's reply (no I/O)
  config     options and the time in the replies
  tracker    what the logger does: counters for the log and the status (no I/O)
  server     the logger connections
  status     the HTTP status endpoint
  __main__   command line, logging, the main loop

Standard library only, Python 3.11 or newer.
"""

__version__ = "0.1.0"
