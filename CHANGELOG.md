# Changelog

## 2.1.0 - unreleased

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
