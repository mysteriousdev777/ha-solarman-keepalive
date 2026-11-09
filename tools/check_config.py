#!/usr/bin/env python3
"""Consistency checks of the app definition (CI): config.yaml, the option translations,
CHANGELOG.md, the Dockerfile base image and the server version must agree.

Needs PyYAML (requirements-dev.txt): a development tool, not part of the app.
"""
from __future__ import annotations

import os
import re
import sys

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "solarman_keepalive")
ARCHES = {"amd64", "aarch64"}
MAP_TYPES = {"app_config", "addon_config", "homeassistant_config", "share", "ssl", "media", "backup",
             "data", "all_app_configs", "all_addon_configs"}


def read(*parts: str) -> str:
    with open(os.path.join(*parts), encoding="utf-8") as f:
        return f.read()


def main() -> int:
    errors: list[str] = []
    cfg = yaml.safe_load(read(APP, "config.yaml"))

    for key in ("name", "version", "slug", "description", "arch"):
        if key not in cfg:
            errors.append(f"config.yaml: missing {key}")
    version = cfg.get("version")
    if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", version):
        errors.append(f"config.yaml: version must be a quoted x.y.z string, not {version!r}")
    if cfg.get("slug") != os.path.basename(APP) or not re.fullmatch(r"[a-z0-9_.-]+", str(cfg.get("slug"))):
        errors.append("config.yaml: slug must match the folder name and use a-z 0-9 _ . -")
    if not set(cfg.get("arch", [])) <= ARCHES or not cfg.get("arch"):
        errors.append(f"config.yaml: arch must be a non-empty subset of {sorted(ARCHES)}")

    options, schema = cfg.get("options", {}), cfg.get("schema", {})
    for key in options:
        if key not in schema:
            errors.append(f"config.yaml: option {key} has no schema")
    for key, kind in schema.items():
        optional = isinstance(kind, str) and kind.endswith("?")
        if not optional and key not in options:
            errors.append(f"config.yaml: required option {key} has no default")

    for m in cfg.get("map", []):
        kind = m.get("type") if isinstance(m, dict) else str(m).split(":")[0]
        if kind not in MAP_TYPES:
            errors.append(f"config.yaml: unknown map type {kind}")

    ports = cfg.get("ports", {})
    if wd := cfg.get("watchdog"):
        m = re.search(r"\[PORT:(\d+)\]", wd)
        if m and f"{m[1]}/tcp" not in ports:
            errors.append(f"config.yaml: watchdog port {m[1]} is not declared in ports")

    tr = yaml.safe_load(read(APP, "translations", "en.yaml"))
    conf, net = tr.get("configuration", {}), tr.get("network", {})
    for key in schema:
        if not (conf.get(key) or {}).get("name") or not (conf.get(key) or {}).get("description"):
            errors.append(f"translations/en.yaml: option {key} needs a name and a description")
    for key in conf:
        if key not in schema:
            errors.append(f"translations/en.yaml: {key} is not an option")
    for port in ports:
        if port not in net:
            errors.append(f"translations/en.yaml: port {port} needs a description")

    m = re.search(r"^## (\S+)", read(APP, "CHANGELOG.md"), re.M)
    if not m or m[1] != version:
        errors.append(f"CHANGELOG.md: the first entry must be ## {version}")
    m = re.search(r'^__version__ = "([^"]+)"', read(APP, "keepalive.py"), re.M)
    if not m or m[1] != version:
        errors.append(f"keepalive.py: __version__ must be {version}")

    m = re.search(r"^ARG BUILD_FROM=(\S+)$", read(APP, "Dockerfile"), re.M)
    if not m:
        errors.append("Dockerfile: needs ARG BUILD_FROM=<image>:<tag> (Supervisor no longer passes it)")
    elif not re.search(r":[\w.-]*\d{4}\.\d{2}\.\d+$", m[1]):
        errors.append(f"Dockerfile: pin the base image to a dated tag, not {m[1]}")

    repo = yaml.safe_load(read(ROOT, "repository.yaml"))
    if not (repo or {}).get("name"):
        errors.append("repository.yaml: missing name")

    for e in errors:
        print("ERROR", e)
    print("config check:", "FAILED" if errors else "ok")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
