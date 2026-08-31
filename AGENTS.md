# Instructions for coding agents

Read [COMPATIBILITY.md](COMPATIBILITY.md) before changing the Home Assistant
version, config flow, options flow, coordinator, entities, diagnostics, HTTP or
MQTT clients, protobuf files, or release metadata.

This integration depends on private EFAPEL Domus40 interfaces. Do not describe
`/HsAPI`, the device-scenario endpoints, MQTT-over-WebSocket transport, or the
protobuf schemas as public or stable. Treat every Home Assistant version change
and every private-protocol change as a compatibility migration.

Never print, commit, or attach live credentials, cookies, CSRF tokens, MQTT
credentials, hostnames, IP addresses, hardware addresses, device identifiers,
device names, raw Home Assistant `.storage` files, packet captures, application
binaries, or decompiler output. Add only synthetic or irreversibly sanitized
fixtures. Keep reverse-engineering inputs under ignored paths.

Preserve these behavioral constraints:

- Home Assistant owns credentials through a normal config entry; never write
  private `.storage` files directly.
- Commands must remain optimistic while authoritative state is reconciled.
- A protobuf mismatch must fall back to REST refreshes rather than guessed
  decoding.
- Identification requests must use the selected logical device ID. Associated
  switch identification reverses mappings for the selected actuator ID only.
- User-visible strings must remain available in English and Portuguese.

Before committing, run `task check`. Protocol or entity changes also require a
sanitized regression test in the exact Home Assistant image. Update
`CHANGELOG.md`, the manifest version, compatibility notes, and translations for
user-visible releases. Do not move an existing tag; tags are immutable inputs
for consumers.

Every commit must follow the
[Conventional Commits](https://www.conventionalcommits.org/) specification, using
the form `<type>[optional scope]: <description>`.

Keep development tooling and reusable GitHub Actions on their latest audited
releases, pin Actions by full commit SHA, and record the release in a comment.
The Home Assistant image is deliberately exempt: it stays at the exact version
in `COMPATIBILITY.md` until the full migration procedure has passed.
