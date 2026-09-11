# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and releases use
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Add an **Enable power measurements** option to pause background power sampling
  while device commands, state updates, and polling remain active. Disabling it
  releases retained reporting leases and clears stale power values. English,
  Portuguese, and Spanish translations are included.
- Distinguish reporting HTTP failures, timeouts, and topic mismatches in
  diagnostics, and retain MQTT rejection packet types and return codes without
  connection details or response bodies.

### Fixed

- Reconnect MQTT only when unknown-message monitoring changes. Power sampling
  and identification options no longer interrupt the state listener.
- Preserve the shared button-mapping task when power sampling is cancelled.
- Preserve periodic polling and queued REST reconciliation during decoded
  state pushes and optimistic commands, so unrelated device traffic cannot
  leave a missed light update stuck indefinitely.
- Reduce metering load with four-device batches, ten-second samples, and a
  thirty-second pause between batches. Pause lease acquisition while MQTT is
  disconnected and release active leases promptly on disconnect.
- Back off failed metering batches and cleanup attempts up to five minutes;
  retain failed cleanup slots until released so leases cannot accumulate.
- Limit all HTTP traffic from one integration client to two simultaneous
  requests, including inventory, metering, commands, authentication, and schemas.
  Concurrent session rejections now share one authentication recovery.
- Stop fast command reconciliation after its deadline when REST reads keep
  failing. Adjust power-reading expiry for the slower metering cycle.
- Expose MQTT connection state and aggregate reporting-release failures in
  privacy-preserving diagnostics.

## [0.10.1] - 2026-09-06

### Fixed

- Reconnect MQTT push updates after a Home Server restart. The push listener
  now retries every connection with the latest session's MQTT credentials and
  topic prefix, forces a re-authentication when the broker rejects a
  connection or subscription, and warns after repeated rejections instead of
  failing silently at debug level.
- Power sensors now report an unknown value when instantaneous readings stop
  arriving instead of presenting the last reading as current indefinitely. A
  reading expires after a bounded multiple of the metering round-robin cycle;
  a local timer updates Home Assistant even when no further state changes
  arrive, without polling the Home Server.
- Validate metering lease topics against the current MQTT prefix after
  authentication, so reporting recovers when a Home Server restart rotates it.
- Cancel the Home Assistant coordinator's refresh scheduler and pending power
  expiry notification when the integration unloads.
- Apply compatible decoded MQTT state events directly to Home Assistant instead
  of performing a full REST refresh after every message.
- Coalesce schema-mismatch and decode-failure fallbacks, cap all full REST
  refreshes to one every five seconds, and use bounded backoff when reconciling
  optimistic commands.
- Surface schema fingerprints, redacted MQTT topic shapes, value-free protobuf
  wire signatures, and refresh/decode counters for troubleshooting without
  retaining private installation data.

## [0.10.0] - 2026-09-03

### Added

- Add an integration-level **Refresh inventory and mappings** action that
  schedules a full config-entry reload after Domus40 inventory, location, or
  emitter-assignment changes.
- Add complete Spanish translations for setup, reauthentication, options,
  errors, and the inventory refresh action.
- Test the sanitized integration contracts against both the supported Home
  Assistant 2026.8.0 minimum and Home Assistant 2026.9.0.

### Changed

- Stop passing Home Assistant's deprecated `suggested_area` device field;
  explicit floor, area, and device registry assignment now owns all Domus40
  placement.
- Replace deprecated identifier-based `via_device` metadata with
  registry-ID-based `via_device_id` reconciliation while preserving separate
  logical-device names and areas on Home Assistant 2026.8 and 2026.9.
- Restructure the documentation around installation and daily use, add a
  complete supported LAN protocol and protobuf reference, and remove
  installation-specific acceptance details.
- Require Conventional Commits and validate commit messages in local checks and
  CI for pull requests and pushes to `main`.

## [0.8.0] - 2026-08-31

### Added

- Import the Domus40 Área → Divisão hierarchy as Home Assistant Floor →
  Area, qualifying division names only when the Home Server contains a global
  name collision.

### Changed

- Migrate devices from a previously shared, unqualified default area to the
  correct qualified area while preserving custom Home Assistant device and
  area-floor assignments.

## [0.7.1] - 2026-08-31

### Fixed

- Register every logical actuator channel as its own linked Home Assistant
  device so secondary channels retain their Domus40 name, state, and division
  instead of appearing under the primary sibling's device and area.

## [0.7.0] - 2026-08-30

### Added

- Native Home Assistant power measurement sensors for each logical row with
  `capMetering`, backed by the verified `DeviceInstantReadingEvent` milliwatt
  field and the Home Server's reporting lease.
- Opt-in unknown MQTT monitoring that records only bounded, redacted topic
  shapes and protobuf field/wire signatures in logs and diagnostics.
- Sanitized reporting and instantaneous-reading emulator contracts.

### Changed

- Baked protobuf revision advanced to `domus40-events-v4`.
- Integration option changes now reconnect only the MQTT listener so observer
  scope changes immediately without rebuilding inventory or emitter mappings.
- Metering leases are sampled in bounded round-robin batches to protect the
  Home Server while preserving each device's latest measurement.
- Initial emitter mapping and metering now run in separate, concurrency-limited
  phases so background discovery does not starve authoritative state polling.

## [0.6.5] - 2026-08-30

### Added

- Native integration options for a 1–30-second identification duration.
- Separate direct actuator, direct switch, and associated-switch identify
  actions.

### Changed

- Identification now targets exact logical IDs instead of expanding requests
  to physical siblings.

## [0.6.4] - 2026-08-27

### Added

- Initial local Home Assistant integration with SSDP/manual setup,
  challenge-response authentication, lights, blinds, outlets, emitters,
  mapping discovery, identification, MQTT push updates, baked protobuf schemas,
  diagnostics, sanitized emulator contracts, and HACS metadata.

[Unreleased]: https://github.com/fiam/home-assistant-domus40/compare/v0.10.0...HEAD
[0.10.0]: https://github.com/fiam/home-assistant-domus40/compare/v0.8.0...v0.10.0
[0.8.0]: https://github.com/fiam/home-assistant-domus40/compare/v0.7.1...v0.8.0
[0.7.1]: https://github.com/fiam/home-assistant-domus40/compare/v0.7.0...v0.7.1
[0.7.0]: https://github.com/fiam/home-assistant-domus40/compare/v0.6.5...v0.7.0
[0.6.5]: https://github.com/fiam/home-assistant-domus40/compare/v0.6.4...v0.6.5
[0.6.4]: https://github.com/fiam/home-assistant-domus40/tree/v0.6.4
