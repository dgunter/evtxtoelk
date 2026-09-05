# Changelog

## 2.2.0 - 2026-09-05

### Changed

- Parsing is now done by the Rust-backed `evtx` package (prebuilt abi3
  wheels for x86-64 and 64-bit ARM on Linux, macOS and Windows, CPython
  3.10+). It parses about 140 times faster than python-evtx, which remains
  the fallback on platforms without a wheel; the two are mutually exclusive
  dependencies because their package names differ only by case and merge on
  case-insensitive filesystems. `--parser` and `EVTXTOELK_PARSER` force a
  backend. Output is identical: the Winlogbeat golden tests pass unchanged.
- The Rust parser recovers more records from damaged files (all five in the
  malformed DNS sample where python-evtx recovered one) and renders GUIDs
  without braces and hex values without padding; braces are restored for
  GUID-typed fields so documents match Winlogbeat either way.
- Positional `EventData` values keep their slot: an empty element is an empty
  `paramN` rather than being skipped.
- Control characters that XML forbids are rendered as `\xHH` text.

### Added

- Tests for the type policy, Community ID, both parser backends (including a
  parity test where both are installed), module edge cases, error paths, and an
  integration test that indexes every fixture module into the generated ECS
  mapping. CI merges coverage from both backend runs.

## 2.1.1 - 2026-09-05

### Added

- ECS fields for events Winlogbeat leaves untouched but that fit directly:
  Windows Filtering Platform events 5150-5159 (`source.*`,
  `destination.*`, `network.transport`, `network.iana_number`,
  `network.direction`, the process, `rule.id`, and connection
  allowed/denied categorisation), registry value changes 4657
  (`registry.path`, `registry.value`, `registry.data.*`), and object access
  events 4656/4658/4660/4663 categorised as file or registry access.

### Changed

- Internal: the field-decoding helpers were split further for readability.

## 2.1.0 - 2026-09-05

### Changed

- Documents are Elastic Common Schema by default, laid out like Winlogbeat:
  `winlog.*` for the record itself, `event.code`, `event.provider`,
  `host.name`, `log.level`, and, for the Security, Sysmon and PowerShell
  channels, the `user.*`, `process.*`, `file.*`, `registry.*`, `network.*`,
  `dns.*` and `powershell.*` fields the Winlogbeat modules derive. Field types
  follow the ECS 9.5.0 and Winlogbeat 9.5 references and every value is
  coerced to its declared type. `--create-index` generates the matching
  mapping. Pass `--legacy` for the 2.0 layout (`Event.System.*`,
  `Event.EventData.Data.*`).
- Documents get a deterministic `_id` from host, channel and record id, so
  reloading a file is idempotent. `--no-dedupe` restores Elasticsearch ids.
- `--ecs-original` includes the record XML as `event.original`.
- Sysmon `@timestamp` is the event's own `UtcTime`; the log write time is
  `event.created`.

### Added

- `evtxtoelk.ecs.to_ecs()`, `ecs_index_body()`, `document_id()`;
  `EvtxToElk(..., ecs=, original=, dedupe=)`; `ensure_index(..., ecs=)`.
- `scripts/build_ecs_tables.py` regenerates the field-type and lookup tables
  from the published references and the pinned Winlogbeat pipelines.
- Fidelity tests against Winlogbeat's own golden documents for 33 Security,
  Sysmon and PowerShell test logs, and typed-output invariants over the whole
  EVTX-ATTACK-SAMPLES corpus.

Known limits of an offline `.evtx`: no rendered `message`; keyword, opcode
and task names only for the standard values and the Security, Sysmon and
PowerShell providers; SIDs resolved for well-known accounts only; no
`dns.question.registered_domain`. See `docs/design-ecs.md`.

## 2.0.0 - 2026-09-05

Rewrite for current Elasticsearch and Python. The documents produced are the
same shape as 1.x with the corrections listed under "Changed".

### Added

- Package layout (`evtxtoelk/`) with a console script, `python -m evtxtoelk`,
  and a small public API: `EvtxToElk`, `make_client`, `ensure_index`,
  `iter_documents`, `transform_event`.
- Authentication and TLS options: `--user`/`--password`, `--api-key`,
  `--ca-certs`, `--insecure` (#7).
- `--create-index` applies a mapping with date/number detection disabled.
- JSON-lines export: `--output FILE`, `--dry-run`, or a destination ending in
  `.json`. Based on the export contributed by @okynos in #4.
- Multiple input files per run.
- Corrupt chunks and records are skipped and counted rather than aborting.
- Test suite (unit, integration against Elasticsearch 9 in Docker, and an
  optional run over the EVTX-ATTACK-SAMPLES corpus), GitHub Actions CI and
  SonarCloud analysis.

### Changed

- Requires Python 3.10+ and the `elasticsearch` 9.x client. Tested against
  Elasticsearch 9.5.
- Bulk actions no longer send `_type`, which Elasticsearch 8+ rejects (#6).
- The destination argument is a URL. Bare `host` and `host:port` still work
  and default to `http://` and port 9200.
- Timestamps carry an explicit UTC offset and are parsed from the formats
  current python-evtx emits.
- `EventData/Data` names have dots replaced with underscores and leading or
  trailing dots removed (#2).
- Records without `EventData` keep the `Event` wrapper and `@timestamp`
  instead of being flattened to the root.
- A single named `Data` element is treated like a list of one, not dumped
  into `RawData`.
- Exit status is `1` only when a bulk item fails or the cluster is unreachable.
- Helper curl scripts moved to `scripts/` and updated to the type-less
  mapping format.
- License changed from MIT to Apache 2.0.

### Removed

- `_type` on indexed documents.
- The `-s` bulk size is now `--bulk-size` (short form kept).

## 1.0.2

Last release of the single-file script. Bulk inserts, metadata, and event
data collapsed to the root of the document.
