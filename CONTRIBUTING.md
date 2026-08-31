# Contributing

Contributions are welcome, particularly sanitized observations from Domus40
installations with device types not covered by the current fixtures.

## Development

Install Docker, [Task](https://taskfile.dev/), and
[uv](https://docs.astral.sh/uv/), then run:

```sh
task check
```

The test task executes inside the exact Home Assistant image declared in the
Taskfile. Ruff is pinned so local and CI results remain reproducible.

## Protocol evidence

The protocol was reverse engineered by monitoring traffic exchanged with the
Home Server on a controlled local network and reproduced with synthetic
fixtures.

Do not submit live captures, credentials, cookies, hostnames, addresses,
identifiers, device names, Home Assistant storage, app binaries, or decompiler
output. Reproduce behavior with synthetic identifiers and documentation-only
network ranges. If evidence cannot be safely sanitized, describe it privately
to the maintainer instead of attaching it to an issue or pull request.

Changes to a private endpoint or protobuf field require:

- a synthetic regression fixture;
- a test that fails without the proposed change;
- a compatibility note explaining the observed contract and fallback;
- live acceptance without secret or installation-specific output.

## Pull requests

Keep changes focused and use
[Conventional Commits](https://www.conventionalcommits.org/) for every commit.
Update English and Portuguese strings together, document user-visible behavior,
and add a changelog entry for release-worthy changes. All checks in the
pull-request template must pass before merge. Existing tags must never be
rewritten.
