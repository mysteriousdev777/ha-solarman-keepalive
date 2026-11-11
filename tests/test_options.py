import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

import helpers  # noqa: F401  (puts the app on sys.path)
from keepalive.__main__ import config_from_args, main, parse_args
from keepalive.config import APP_FRAME_LOG, Config, ConfigError, load_options
from keepalive.server import log


class OptionsTest(unittest.TestCase):
    def load(self, options) -> "Config":
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "options.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write(options if isinstance(options, str) else json.dumps(options))
            return load_options(path)

    def test_defaults(self):
        c = self.load({})
        self.assertEqual((c.time, c.timezone, c.allowed_ips, c.idle_timeout, c.log_level, c.frame_log),
                         ("local", "", [], 15, "info", None))
        self.assertEqual((c.port, c.status_port), (10000, 8080))

    def test_app_options(self):
        c = self.load({"time": "utc", "timezone": " Europe/Kyiv ", "allowed_ips": ["192.0.2.10 "],
                       "idle_timeout": 0, "log_level": "debug", "frame_log": True})
        self.assertEqual((c.time, c.timezone, c.allowed_ips, c.idle_timeout, c.log_level),
                         ("utc", "Europe/Kyiv", ["192.0.2.10"], 0, "debug"))
        self.assertEqual(c.frame_log, APP_FRAME_LOG)

    def test_invalid(self):
        for bad in ({"time": "cloud"}, {"log_level": "loud"}, {"idle_timeout": -1}, {"idle_timeout": 5000},
                    {"idle_timeout": True}, {"allowed_ips": ["logger"]}, {"allowed_ips": "192.0.2.10"},
                    "[1, 2]", "not json"):
            with self.subTest(bad=bad), self.assertRaises(ConfigError):
                self.load(bad)

    def test_missing_file(self):
        with self.assertRaises(ConfigError):
            load_options(os.path.join(tempfile.gettempdir(), "no-such-dir-ka", "options.json"))

    def test_app_without_time_zone_exits_2(self):
        """The time zone is checked first: the CI container test greps for this message."""
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "options.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"time": "local", "frame_log": True}, f)    # frame log dir missing too
            out = io.StringIO()
            with mock.patch.dict(os.environ, {}, clear=True), contextlib.redirect_stdout(out):
                self.assertEqual(main(["--options", path]), 2)
        for h in list(log.handlers):
            log.removeHandler(h)
        self.assertIn("ERROR time 'local' needs a time zone", out.getvalue())

    def test_command_line(self):
        a = parse_args(["--time", "utc", "--allow", "192.0.2.10", "--idle-timeout", "5", "--no-status",
                          "--host", "127.0.0.1"])
        c = config_from_args(a)
        self.assertEqual((c.time, c.allowed_ips, c.idle_timeout, c.status_port, c.host),
                         ("utc", ["192.0.2.10"], 5, None, "127.0.0.1"))
        self.assertEqual(config_from_args(parse_args([])).host, "0.0.0.0")


if __name__ == "__main__":
    unittest.main()
