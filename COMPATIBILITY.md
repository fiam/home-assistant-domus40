# Compatibility contract

The integration intentionally uses private Home Assistant and EFAPEL Domus40
interfaces. A successful import is not sufficient proof of compatibility.

## Verified baseline

| Contract | Verified value |
| --- | --- |
| Integration release | `0.10.1` |
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
high-concurrency workloads at the same time. Options apply through the
config-entry update listener and do not require a full integration reload.
Only changes to unknown-message monitoring reconnect MQTT.

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

The private SSDP UUID can change after a Home Server restart. Unreleased
discovery matching checks the original config-entry identity, the last observed
`server_udn`, then the normalized API origin. A matching origin allows the new
discovery UUID to be remembered through Home Assistant's config-entry API while
preserving the original entry unique ID and all derived registry identities.
The remembered UUID can then follow a later address change. Simultaneous UUID
and address changes cannot be linked automatically without another verified
identity. Matching display names alone never merges hubs. Duplicate discovery,
manual setup, and selected-server setup are checked before authentication;
ignored discoveries are left unchanged. Sanitized regressions use the real
config-entry manager in both exact Home Assistant images. Existing translated
`already_configured` text is reused.
Live acceptance on 2026.9.0 confirmed a changed advertised UUID at the configured
origin, persistence of the new discovery alias through HA, removal of the
duplicate discovery flow, and unchanged config-entry and entity-registry
identities and options.

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

Version 0.10.1 was validated with 63 sanitized tests in each exact Home
Assistant image above. Tests through Home Assistant's real coordinator confirm
that a burst of 200 decoded state events issues no REST requests, while 200
schema-mismatch events coalesce into one fallback refresh and respect the
five-second minimum after the previous poll. Emulator contracts cover rejected
MQTT credentials followed by authenticated reconnection; coordinator contracts
also cover a topic-prefix change during metering lease activation.

Instantaneous readings expire through a shared event-loop timer that notifies
entities without requesting REST state. Listener-level tests verify that a
silent push pipeline becomes unknown, a new reading postpones expiry, and
unloading cancels expiry notifications. Shutdown also delegates to Home
Assistant's coordinator cleanup to cancel scheduled refreshes. The existing
English, Portuguese, and Spanish catalogs still pass the structural contract;
this patch introduces no new user-interface translation keys.

## Unreleased load controls

Unreleased changes reduce reporting batches from eight to four logical devices
and add a thirty-second pause after each ten-second sample.
Failed batches or cleanup double the pause to at most five minutes. Cleanup
failures retain their lease slots until release succeeds. Metering waits for a
subscribed MQTT connection, interrupts sampling on disconnect, and tracks
completed activations individually so unloading a partial batch releases them.
Reading freshness includes the normal pause in the round-robin cycle.

All HTTP requests from one client, including schema downloads and session
recovery, share two concurrency slots. Network timeouts exclude time waiting
for a slot. HTTP session generations coalesce concurrent expired-session
responses into one authentication recovery. Failed command reads no longer
keep fast reconciliation alive after the command deadline.

These are scheduling changes to the existing private contracts; request
payloads, schema validation, logical-device attribution, optimistic commands,
and the full-REST-refresh limit remain unchanged. Sanitized regressions cover
aggregate HTTP concurrency, shared authentication recovery, MQTT disconnect and
reconnect, reporting cadence/backoff, failed cleanup, partial-batch unload, and
command expiry during an outage in the exact supported Home Assistant images.
An emulator measurement with eight synthetic metering devices reduced steady
lease traffic from about 94 to 12 HTTP requests per minute; an overlapping full
refresh and metering batch peaked at two HTTP requests instead of five.
The power-sampling option is translated into English, Portuguese, and Spanish;
all three catalogs remain covered by the structural contract. Physical Home
Server load acceptance is still pending before release; the emulator cannot
establish hub capacity.
The manifest and verified release baseline remain at 0.10.1 until release.

The `enable_metering` config-entry option defaults to true to preserve existing
installations. Disabling it cancels sampling, immediately clears power readings,
and releases completed leases in the background, retaining failed releases for
bounded retries even without MQTT. State decoding, commands, and periodic
polling remain active. Re-enabling resumes bounded batches after retained leases
are cleaned up. Cancelling sampling must not cancel the shared initial mapping
task. Only changing the observer scope reconnects MQTT; metering and identify
options preserve the current connection. Sanitized contracts cover disabled
startup, live pause/resume, partial batches, cleanup failures, sensor
notifications, and the native options form in both exact images.

Partial state updates now notify coordinator listeners without resetting the
periodic REST timer or cancelling queued refresh requests. Home Assistant's
`async_set_updated_data` does both in the exact 2026.8.0 and 2026.9.0 images;
using it for one device's push or optimistic command could indefinitely defer
recovery of another device's missed state event. Synthetic regressions exercise
the real coordinator scheduler with continuous unrelated push traffic and
queued REST refreshes, preserving optimistic state and the five-second full
refresh limit. Contract-test containers have external networking disabled;
their emulator traffic stays on loopback. Polling still depends on the Home
Server returning a current inventory; it cannot repair stale state inside the
Home Server itself.

Reporting diagnostics retain bounded reason counters and activation attempts,
including unsuccessful HTTP status codes, invalid/mismatched topics,
authentication failures, and timeouts. MQTT rejection diagnostics retain only
CONNACK/SUBACK return codes. Response bodies, exception messages from transports,
topics, credentials, and logical identifiers are excluded. Sanitized emulator
contracts verify HTTP status attribution and MQTT credential rejection codes;
coordinator tests distinguish HTTP failure from a mismatched reporting topic
and retain failure accounting when batches are interrupted.

A bounded live check on 2026.9.0 observed successful state and button decoding,
continued periodic polling, and HTTP 412 reporting rejections while MQTT remained
connected. The native options flow paused sampling and cleared power values
without making entities unavailable. This validates those integration paths;
it does not establish the Home Server's capacity or the private condition behind
the HTTP 412 responses. Sustained physical load acceptance remains pending.

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
