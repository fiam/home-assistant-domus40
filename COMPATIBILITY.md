# Compatibility contract

The integration intentionally uses private Home Assistant and EFAPEL Domus40
interfaces. A successful import is not sufficient proof of compatibility.

## Verified baseline

| Contract | Verified value |
| --- | --- |
| Integration release | `0.10.0` |
| Minimum supported Home Assistant Core | `2026.8.0` |
| Latest verified Home Assistant Core | `2026.9.0` |
| Home Assistant container Python | `3.14` |
| Protocol schema revision | `domus40-events-v4` |
| Discovery | SSDP plus manual LAN address fallback |
| Entities | lights, blinds, outlets, power sensors, wall/IR events, identify actions; Área → Divisão maps to Floor → Area |
| Local state | 30-second polling plus MQTT-over-WebSocket state and metering push |

The sanitized contract suite passes in both exact Home Assistant images above.
The supported 2026.8 minimum remains a permanent compatibility lane alongside
the latest verified stable release. Exact tags are pinned deliberately and are
advanced only after the imported Home Assistant APIs and contracts have been
re-audited; a moving stable tag is never used.

The sanitized emulator and contract tests cover authentication, inventory,
device scenarios, commands, identification, reporting leases, MQTT framing,
binary/base64 state and instantaneous-power payloads, optimistic write
reconciliation, and config-entry options. Redacted Home Server acceptance
covers the same user-facing paths without retaining installation names,
addresses, identifiers, topology, or counts.

Reporting acceptance established that readings are attributed by logical
`deviceId`, excessive concurrent leases can be rejected, and same-hardware rows
must be separated across bounded round-robin batches to keep the Home Server
responsive. Initial emitter mapping and metering must not run as
high-concurrency workloads at the same time. Options reconnect MQTT through the
config-entry update listener and do not require a full integration reload.

Logical actuator siblings are registered as separate linked Home Assistant
devices because the Home Server may assign their channels to different
divisions. Collapsing siblings under a primary row can therefore produce an
incorrect area and a misleading combined device/entity name. Upgrades
explicitly migrate existing entity-registry rows to newly split devices before
platform setup; changing `device_info` alone does not move an existing Home
Assistant entity. Version 0.10.0 also replaces the deprecated identifier-based
`via_device` metadata with registry-ID-based `via_device_id` reconciliation
after platform setup. The same path runs on Home Assistant 2026.8 and 2026.9 so
both versions retain identical logical-device grouping and area behavior.

Version 0.8.0 reads the Home Server's parent-area inventory and maps Domus40
Área → Divisão to Home Assistant Floor → Area. Sanitized contracts pin the
`division.area` reference, preserve globally unique division names, qualify
only colliding names, migrate devices from the old shared default area, and
preserve custom device-area or non-empty area-floor choices. This uses the
pinned Home Assistant floor, area, and device registry callbacks; their
signatures must be re-audited on every Home Assistant upgrade. Device placement
is explicit and does not use Home Assistant's deprecated `suggested_area`
device field.

Version 0.10.0 adds an integration-level inventory and mapping refresh action.
It uses Home Assistant's pinned `async_schedule_reload` config-entry callback
so the action returns promptly while the normal setup path rebuilds inventory,
location hierarchy, entity platforms, metering capabilities, and emitter
mappings together. This callback remains a version-coupled Home Assistant
interface and must be re-audited during upgrades. Sanitized acceptance covers a
single refresh action, its available → unavailable → available reload lifecycle,
registry preservation, and restoration of enabled runtime entities.

English, Portuguese, and Spanish catalogs cover the same config-flow, options,
error, abort, and integration-action keys; a structural contract test prevents
language catalogs from drifting.

## Private contracts

- Cookie and challenge authentication below `/HsAPI/authentication`.
- Device/area/division inventory and scenario assignment endpoints.
- State and identification writes below `/HsAPI/devices`.
- Short-lived per-device reporting leases below `/HsAPI/devices/{id}/reporting`.
- MQTT 3.1.1 over WebSocket on the Home Server's local port 9998.
- Private protobuf schemas retrieved from the authenticated Home Server.
- Home Assistant config-flow, typed config-entry, coordinator, entity,
  floor/area/device/entity registry migration, diagnostics, SSDP, and aiohttp
  interfaces at the exact pinned release.

Any of these may change independently. Schema incompatibility must disable
decoded push updates and use MQTT only as a signal for an authoritative REST
refresh. Compatible decoded state messages update the in-memory snapshot
directly and are protected briefly from a lagging REST readback. Schema
mismatches, malformed messages, unhandled state messages, command
reconciliation, periodic polling, and explicit refresh requests all share a
global limit of one full REST refresh every five seconds. Troubleshooting logs
and diagnostics expose only schema fingerprints, redacted topic shapes,
value-free protobuf wire signatures, and aggregate counters.

## Migration procedure

1. Select an exact Home Assistant patch release; never test against a moving
   `stable` tag.
2. Audit the imported Home Assistant APIs and their upstream tests at that tag.
3. Update the Taskfile, CI image, compatibility table, and any affected code.
4. Run `task check` with synthetic fixtures.
5. Test a fresh config entry against a disposable Home Assistant instance.
6. Perform redacted Home Server acceptance for every affected device/protocol
   path without retaining installation-specific output.
7. Update the manifest version and changelog before tagging a release.

Never use a production Home Assistant volume as the first migration test.
