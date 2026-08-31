# Domus40 protocol emulator

This emulator serves only synthetic identifiers, credentials, inventory, and
state. It exercises the authentication and LAN protocol observed in controlled
Home Server traffic: REST inventory and commands, reporting leases, the baked
`DeviceStateEvent` and
`DeviceInstantReadingEvent` schemas, and MQTT-over-WebSocket framing. It does
not contain a capture from a real installation.

Run it from the repository root:

```sh
docker run --rm -it \
  -p 80:8080 -p 9998:8080 \
  -v "$PWD:/work" -w /work \
  python:3.13-slim sh -c \
  'pip install --quiet -r domus40_emulator/requirements.txt && python domus40_emulator/server.py'
```

Use `fixture-admin` / `fixture-password`. The production integration still
requires SSDP discovery, so use this server directly for API contract tests or
inject a sanitized `SsdpServiceInfo` in a Home Assistant config-flow test. The
same aiohttp application exposes HTTP and WebSocket routes; a full LAN emulator
can place the WebSocket route behind port 9998 with a local reverse proxy.

Pass `--base64-events` to exercise the alternate payload representation used
by the shipped browser decoder.

The reporting route returns a synthetic per-device instantaneous-reading topic.
Tests can publish a synthetic milliwatt value through the emulator's
`publish_power` helper after acquiring that device's reporting lease.
