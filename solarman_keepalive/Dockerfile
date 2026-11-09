# Home Assistant base image, pinned to a dated tag: Python 3.14.10 on Alpine 3.24, tzdata included.
# Change it deliberately, together with the tests (see the repository README).
ARG BUILD_FROM=ghcr.io/home-assistant/base-python:3.14-alpine3.24-2026.10.3
FROM ${BUILD_FROM}

WORKDIR /app
COPY keepalive.py /app/keepalive.py

# The server runs directly under Docker's init (config.yaml: init: true), not under the
# image's S6 overlay: one process, standard library only, nothing else to supervise.
ENTRYPOINT []
CMD ["python3", "-u", "/app/keepalive.py", "--options", "/data/options.json"]
