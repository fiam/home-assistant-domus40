# Domus40 Home Server LAN protocol

This document describes the private LAN interfaces used by the integration.
The protocol was reverse engineered by monitoring traffic exchanged with an
EFAPEL Domus40 Home Server on a controlled local network, then reproduced with
synthetic requests and fixtures. No packet captures, credentials, addresses,
device identifiers, or installation data are included in this repository.

The interfaces are private, unsupported, and version-coupled. They must not be
treated as a public EFAPEL API. The integration verifies the contracts it
depends on and falls back to authoritative REST polling when a protobuf schema
is incompatible.

## Architecture

The integration communicates only with the **Home Server D40 (reference
40930)** on the LAN:

1. SSDP locates a compatible Home Server, or the user provides its LAN address.
2. HTTP requests under `/HsAPI` create an authenticated cookie session.
3. REST inventory supplies areas, divisions, logical device rows, and scenario
   assignments.
4. REST writes change actuator state, identify devices, and manage temporary
   metering-reporting leases.
5. MQTT 3.1.1 over WebSocket supplies protobuf state, button, and metering
   events.

This integration does not connect to EFAPEL/Domus40 cloud services. That claim
applies to the integration, not to other features or configuration of the Home
Server itself.

## Discovery

The Home Server advertises `upnp:rootdevice` over SSDP. A discovery is accepted
when its UPnP description has manufacturer `EFAPEL` and a `deviceType` beginning
with `urn:schemas-upnp-org:device:HomeServer-`.

The SSDP description can use a dedicated UPnP port. The integration takes the
discovered host and uses HTTP port 80 for `/HsAPI`. Manual configuration accepts
an HTTP LAN origin or a hostname/IP address that can be normalized to one.

## HTTP session and authentication

All paths below are relative to `/HsAPI`.

1. `GET /users/valid` primes the session. The `X-AntiCSRF` response header is
   retained.
2. `POST /authentication` sends `username` and `csrftoken`.
3. An unauthenticated session returns a challenge. The response is:

   ```text
   password_hash = SHA256_HEX(password)
   double_hash   = SHA256_HEX(password_hash)
   response      = SHA256_HEX(challenge + double_hash)
   ```

4. A second `POST /authentication` sends `username`, `challengeResponse`, and
   `csrftoken`. The returned cookie authenticates subsequent calls. The response
   also contains MQTT connection fields.
5. `DELETE /authentication` performs a best-effort logout.

The integration retries authentication once after an HTTP 403. Credentials,
cookies, CSRF values, challenges, and MQTT connection fields are never exposed
in diagnostics.

## REST contract

| Purpose | Method and path | Relevant input/output |
| --- | --- | --- |
| Device inventory | `GET /devices/getDevices` | Logical actuator and emitter rows, capabilities, level/state, type, division, and physical sibling address |
| Divisions | `GET /divisions/getDivisions` | Division ID, name, and parent area reference |
| Parent areas | `GET /areas/getAreas` | Area ID and name |
| Set output | `PUT /devices/{id}/state` | JSON `level`, clamped to 0–100 |
| Identify logical row | `PUT /devices/{id}/led` | JSON `duration`, clamped to 1–30 seconds |
| Start metering | `PUT /devices/{id}/reporting` | Empty JSON request; response contains a temporary MQTT topic |
| Stop metering | `DELETE /devices/{id}/reporting` | Releases the reporting lease |
| Scenario index | `GET /devicescenarios/?deviceId={id}` | Endpoint-to-scenario assignments for one emitter |
| Single endpoint assignment | `GET /devicescenarios/?deviceId={id}&deviceEndpoint={endpoint}` | Scenario assignment for one emitter endpoint |
| Scenario actions | `GET /scenarios/full/{scenarioId}` | Actions, targets, and levels associated with an assignment |
| Protobuf schemas | `GET /static/protos/{filename}` | Authenticated `events.proto`, `constants.proto`, or `common_header.proto` |

Inventory requests order devices by `DEV_SET_DISPLAY_NAME` using
`AscNoCase`. The integration tolerates unknown device types and incomplete
optional values rather than treating them as actionable entities.

### Inventory model

The server inventory consists of logical rows. Rows with `headerFilter` equal
to `Actuators` represent outputs. Rows with `headerFilter` equal to `Switches`
represent emitters when their button capabilities are present. Multiple rows
may share a physical hardware address while retaining independent names,
locations, IDs, and capabilities.

Each logical actuator channel therefore remains a separate Home Assistant
device. A named emitter is also separate and can be linked to a physical
actuator sibling through Home Assistant's device registry. Entity
identity is based on the logical row ID, never on a display name or network
address.

Domus40 Área → Divisão maps to Home Assistant Floor → Area. Globally unique
division names remain unchanged; only collisions are qualified with their
parent floor.

### Scenario mappings and identification

Wall endpoints `TeclaA` through `TeclaD`, and optionally the sixteen IR
endpoints, can refer to ordinary Home Server scenarios. The integration reads
those scenarios to display existing assignments and emit button events. It does
not execute scenario actions itself.

Identification always uses an exact logical row ID:

- actuator identification addresses the selected actuator row;
- switch identification addresses the selected emitter row;
- associated-switch identification reverses the loaded scenario mappings and
  identifies each emitter whose action targets the selected actuator ID.

Physical siblings are not implicitly broadened into an identification request.

### State reconciliation

REST inventory is authoritative but can temporarily lag behind a successful
write. The integration applies light, outlet, and blind commands optimistically
and reconciles REST state with bounded backoff. Compatible decoded push events
update the in-memory state immediately and can settle a pending command without
a REST request. Their values are protected briefly from a lagging REST readback.
All full REST refreshes share a global limit of one every five seconds.

For `LightingRegulator`, `levelPercentage` is the authoritative on/off and
brightness value because `switchedOn` is not consistently authoritative for
that device type.

## MQTT over WebSocket

The Home Server exposes MQTT 3.1.1 at `ws://<home-server>:9998/` using WebSocket
subprotocol `mqttv3.1`. The authentication response supplies a topic prefix,
MQTT username, and MQTT password.

The client sends a standard MQTT 3.1.1 CONNECT packet, subscribes at QoS 0, and
handles fragmented WebSocket byte streams, PUBLISH, SUBACK, PINGREQ/PINGRESP,
and QoS 1 acknowledgements. The normal topic filters, appended to the returned
prefix, are:

| Topic filter | Payload |
| --- | --- |
| `events/device/+/state/changed` | `DeviceStateEvent` |
| `events/device/+/reading/instant` | `DeviceInstantReadingEvent` |

The metering filter remains subscribed while the MQTT connection is active;
readings are emitted only for logical rows with active reporting leases.
Optional unknown-message monitoring replaces the known filters with `#` below
the private prefix and retains only bounded, redacted topic shapes and protobuf
field/wire signatures.

## Protobuf transport and decoding

The Home Server serves proto2 schemas only after authentication. The integration
bakes the supported subset beside its source so startup and safe fallback do
not depend on dynamic code generation. The canonical files are:

- [`common_header.proto`](../custom_components/domus40/common_header.proto)
- [`events.proto`](../custom_components/domus40/events.proto)
- [`constants.proto`](../custom_components/domus40/constants.proto)

The current supported revision is `domus40-events-v4`. MQTT payloads can be raw
protobuf bytes or a base64 representation. The dependency-free decoder accepts
both, decodes only the fields listed below, and skips unknown varint, 32-bit,
64-bit, and length-delimited fields. Malformed payloads are rejected.

At setup, the integration retrieves the authenticated server schemas and
compares the required field labels, types, numbers, and consumed enum values to
the baked contract. State and metering compatibility are evaluated separately:

- compatible state schemas enable decoded output and button updates;
- incompatible state schemas use MQTT only as a change signal and trigger a
  coalesced, rate-limited REST refresh;
- compatible instantaneous-reading schemas enable native power sensors;
- incompatible metering schemas leave metering disabled rather than guessing.

Decode warnings retain only redacted topic shapes and value-free protobuf
field/wire signatures. Schema warnings and diagnostics retain only schema hash
prefixes, non-sensitive field contracts, and aggregate counters.

## Complete supported protobuf contract

This section includes every message and enum in the baked, validated contract.
Types present in a server schema but not consumed by the integration are not
part of this supported contract and are deliberately not reproduced or guessed.

### `MessageInfo`

| Label | Type | Field | Number | Use |
| --- | --- | --- | ---: | --- |
| `required` | `int64` | `message_seq` | 1 | Envelope sequence metadata; skipped by the integration |
| `required` | `string` | `originator` | 2 | Envelope origin metadata; skipped by the integration |
| `required` | `int64` | `timestamp` | 3 | Envelope timestamp metadata; skipped by the integration |

### `DeviceStateEvent`

| Label | Type | Field | Number | Default/meaning |
| --- | --- | --- | ---: | --- |
| `required` | `MessageInfo` | `info` | 1 | Event metadata |
| `required` | `int64` | `deviceId` | 2 | Logical device row |
| `optional` | `DeviceEndpoint` | `endpoint` | 100 | Defaults to `NoValue` |
| `optional` | `ButtonState` | `buttonState` | 101 | Button transition |
| `optional` | `RegulatorState` | `energyChangeState` | 200 | Regulator movement state |
| `optional` | `uint32` | `energyLevel` | 201 | Level from 0 through 100 |
| `optional` | `uint32` | `timeActive` | 301 | Reported active time |

### `DeviceInstantReadingEvent`

| Label | Type | Field | Number | Meaning |
| --- | --- | --- | ---: | --- |
| `required` | `MessageInfo` | `info` | 1 | Event metadata |
| `required` | `int64` | `deviceId` | 2 | Logical device row |
| `optional` | `int64` | `powerMeasured_mA` | 10 | Current measurement in milliamperes |
| `optional` | `int32` | `powerFactor` | 11 | Power factor value reported by the server |
| `optional` | `int64` | `consumed_mW` | 12 | Instantaneous power in milliwatts; divided by 1000 for watts |
| `optional` | `int32` | `voltage_V` | 13 | Voltage in volts |
| `optional` | `double` | `temperature` | 20 | Temperature value |
| `optional` | `double` | `externalTemperature` | 21 | External temperature value |
| `optional` | `int32` | `luminance` | 22 | Luminance value |

### `DeviceEndpoint`

| Name | Value | Name | Value |
| --- | ---: | --- | ---: |
| `NoValue` | 0 | `DeviceIdentification` | 1 |
| `SmartEnergy` | 2 | `EFAPEL` | 3 |
| `AtuadorOnOff1` | 4 | `AtuadorOnOff2` | 5 |
| `AtuadorOnOffPlug` | 6 | `ControladorDePersiana` | 7 |
| `ReguladorDeLuz` | 8 | `TeclaA` | 9 |
| `TeclaB` | 10 | `TeclaC` | 11 |
| `TeclaD` | 12 | `TeclaIRA` | 13 |
| `TeclaIRB` | 14 | `TeclaIRC` | 15 |
| `TeclaIRD` | 16 | `TeclaIRE` | 17 |
| `TeclaIRF` | 18 | `TeclaIRG` | 19 |
| `TeclaIRH` | 20 | `TeclaIR1Up` | 21 |
| `TeclaIR1Down` | 22 | `TeclaIR2Up` | 23 |
| `TeclaIR2Down` | 24 | `TeclaIR3Up` | 25 |
| `TeclaIR3Down` | 26 | `TeclaIR4Up` | 27 |
| `TeclaIR4Down` | 28 | `Multifuncoes` | 29 |

### `ButtonState`

| Name | Value | Integration event type |
| --- | ---: | --- |
| `Pressed` | 0 | `pressed` |
| `Released` | 1 | `released` |

### `RegulatorState`

| Name | Value |
| --- | ---: |
| `Stopped` | 0 |
| `GoingUp` | 1 |
| `GoingDown` | 2 |
| `SettingLevel` | 3 |

## Metering leases

Instantaneous readings require a temporary reporting lease per logical row.
The Home Server can reject excessive concurrent leases. The integration
therefore samples a bounded number of targets for a short period, releases
them, and continues round-robin. Rows sharing a physical address are placed in
different batches while each reading remains attributed by its logical
`deviceId`.

The private history endpoint has not been verified as a monotonic lifetime
total, so its interval aggregates are not represented as Home Assistant
`total_increasing` energy sensors.

## Compatibility and evidence

Any endpoint, response shape, topic, or protobuf field may change independently.
A protocol change requires a synthetic regression fixture, a failing-then-
passing contract test, a compatibility note, and redacted acceptance against a
Home Server. Never add raw traffic, addresses, credentials, device identifiers,
device names, application binaries, or other installation-specific evidence to
the repository.

The sanitized emulator exercises authentication, REST inventory and commands,
reporting leases, both supported event messages, and MQTT-over-WebSocket
framing. See [`domus40_emulator/README.md`](../domus40_emulator/README.md).
