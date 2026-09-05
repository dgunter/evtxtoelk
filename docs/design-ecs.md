# Design: Elastic Common Schema output for evtxtoelk

Status: accepted 5 September 2026; implemented in 2.1.0 (all phases).

## Goal

Add an opt-in `--ecs` mode that turns each `.evtx` record into a document laid
out the way Winlogbeat lays it out, so evtxtoelk output works with Elastic's
prebuilt Windows detection rules, the Security app, ECS dashboards, and the
Zeek data ParseZeekLogs already produces in ECS form. Every emitted value must
carry the type its field is declared with; the index mapping must be generated
from the same table so documents and mapping never disagree.

## Sources of truth

Field names and types come from the published references, pinned by version:

| Reference | Version | Used for |
| --- | --- | --- |
| [ECS field reference](https://www.elastic.co/docs/reference/ecs/ecs-field-reference), machine-readable as the `generated/ecs/ecs_flat.yml` release artifact | ECS 9.5.0 (4 Aug 2026), 2,613 fields | types, array-ness and allowed values of every ECS field |
| [Winlogbeat exported fields, winlog](https://www.elastic.co/docs/reference/beats/winlogbeat/exported-fields-winlog) | Beats 9.5 | the `winlog.*` layout: 157 fields, 129 of them `winlog.event_data.*` |
| Winlogbeat exported fields for the [security](https://www.elastic.co/docs/reference/beats/winlogbeat/exported-fields-security), [sysmon](https://www.elastic.co/docs/reference/beats/winlogbeat/exported-fields-sysmon) and [powershell](https://www.elastic.co/docs/reference/beats/winlogbeat/exported-fields-powershell) modules | Beats 9.5 | the module-specific `winlog.logon.*`, `sysmon.*`, `powershell.*` fields |

The Winlogbeat module ingest pipelines are consulted only for behaviour the
references leave undefined, mainly which `event.action`, `event.category`,
`event.type` and `event.outcome` a given event ID receives and which
`event_data` values populate `user.*`, `process.*`, `file.*`, `registry.*`,
`network.*` and `dns.*`. The design says where that happens.

The same approach worked for ParseZeekLogs: `scripts/build_ecs_tables.py`
fetches the pinned artifacts, emits a data-only `_ecs_tables.py`, and the
transform, the type coercion and `ecs_index_body()` all read from it.

## What an evtx record gives us

python-evtx renders each record as the Windows event XML. After `xmltodict`
the pieces are:

| XML | Content | Notes |
| --- | --- | --- |
| `System/Provider/@Name`, `@Guid`, `@EventSourceName` | provider | `EventSourceName` present for classic (pre-Vista) providers |
| `System/EventID/#text`, `@Qualifiers` | numeric event ID | qualifiers only on classic providers |
| `System/Version`, `Level`, `Task`, `Opcode`, `Keywords` | numeric | `Keywords` is a hex bitmask string such as `0x8020000000000000` |
| `System/TimeCreated/@SystemTime` | timestamp | microsecond precision after parsing; Windows stores 100 ns ticks |
| `System/EventRecordID` | record number | unique within a channel on one host |
| `System/Correlation/@ActivityID`, `@RelatedActivityID` | GUIDs, often empty | |
| `System/Execution/@ProcessID`, `@ThreadID` | logging process | decimal strings |
| `System/Channel`, `Computer` | channel and host | `Computer` is usually the FQDN |
| `System/Security/@UserID` | SID of the logging account | often empty |
| `EventData/Data[@Name]` | named values | the bulk of the payload; already collapsed to a dict by the current transform |
| `EventData/Data` without names, `EventData/Binary` | positional or binary payload | classic providers and a few modern ones |
| `UserData/*` | provider-defined XML | used by RDP, TerminalServices, BITS, Defender and others |

Three things Winlogbeat has and an offline evtx does not:

- `message`, the rendered text. It is assembled on the source host from the
  provider's message DLL using the `EventData` values as inserts. Not
  available offline and out of scope.
- `winlog.keywords`, `winlog.opcode` and `winlog.task` as names. The evtx
  holds the numbers; names come from the provider manifest. Security-channel
  values are stable and will be tabled (`Audit Success`, `Audit Failure`, the
  task categories); other providers keep the numeric form.
- Account name resolution for `winlog.user.*` when only a SID is present.
  Well-known SIDs will be tabled; everything else keeps the SID in
  `winlog.user.identifier`.

## What the corpus contains

EVTX-ATTACK-SAMPLES, the corpus the loader tests already run over: 278 files,
37,364 records, 148 distinct channel and event ID pairs.

| Channel | Records | Note |
| --- | --- | --- |
| no channel (`Microsoft-Windows-RPC` debug trace) | 29,524 | one file; generic layer only |
| `Microsoft-Windows-Sysmon/Operational` | 3,241 | IDs 1, 7, 13, 11, 3, 10, 8, 12, 18, 5, 17, 6, 2, 20, 21 |
| `Security` | 1,577 | IDs 5145, 4663, 5156, 4624, 4688, 5136, 4662, 1102, 4672, 4661, 5158, 4776, 4768, 4719, 4742 |
| `Microsoft-Windows-Bits-Client/Operational` | 1,548 | `UserData` payloads |
| `Microsoft-Windows-RemoteDesktopServices-RdpCoreTS/Operational` | 794 | `UserData` payloads |
| `Application` | 393 | mostly `MsiInstaller`, classic provider |
| `Microsoft-Windows-TerminalServices-RemoteConnectionManager/Operational` | 228 | `UserData` payloads |
| `Microsoft-Windows-PowerShell/Operational`, `Windows PowerShell` | 11 | IDs 4104, 800, 40961, 40962, 53504 |

So Sysmon and Security carry the value, PowerShell is thin in this corpus but
important in practice, and a good share of records are `UserData` events that
only the generic layer will touch. Winlogbeat's published golden files (one
JSON document per tested event ID under each module's `test/testdata/ingest/`)
cover the Security and Sysmon IDs the corpus lacks and will serve as fixtures.

## Document layout

### Layer 1: generic, every record

Applied to every event regardless of provider. Types are the published ones.

| Target | Type | Source | Conversion |
| --- | --- | --- | --- |
| `@timestamp` | date | `TimeCreated/@SystemTime` | ISO-8601 UTC; microseconds kept |
| `event.created` | date | same | Winlogbeat sets it to the read time; offline the event time is the closest value, and dashboards expect the field |
| `event.kind` | keyword | constant `event` | `alert` for Defender detections is a module decision, not generic |
| `event.code` | keyword | `EventID/#text` | string, never a number |
| `event.provider` | keyword | `Provider/@Name` | |
| `event.module` | keyword | `security`, `sysmon`, `powershell`, else absent | set by the routing step below |
| `event.dataset` | keyword | `<module>.<channel slug>` e.g. `sysmon.operational`, `security.security`; generic records get `windows.<channel slug>` | slug lowercases and replaces `/` and spaces with `.` and `_` |
| `event.original` | keyword | raw record XML | opt-in flag; roughly doubles document size |
| `host.name` | keyword | `Computer` | |
| `log.level` | keyword | `Level` | 0 → `information` (LogAlways), 1 → `critical`, 2 → `error`, 3 → `warning`, 4 → `information`, 5 → `verbose` |
| `winlog.channel` | keyword | `Channel` | |
| `winlog.event_id` | keyword | `EventID/#text` | string |
| `winlog.provider_name` | keyword | `Provider/@Name` | |
| `winlog.provider_guid` | keyword | `Provider/@Guid` | uppercase inside braces, as Windows renders it |
| `winlog.record_id` | keyword | `EventRecordID` | string per the spec, despite being numeric |
| `winlog.computer_name` | keyword | `Computer` | |
| `winlog.time_created` | date | `TimeCreated/@SystemTime` | same value as `@timestamp` |
| `winlog.version` | long | `Version` | integer |
| `winlog.opcode` | keyword | `Opcode` | name when tabled for the provider, else the number as a string |
| `winlog.task` | keyword | `Task` | same rule |
| `winlog.keywords` | keyword, array | `Keywords` | bitmask decoded to names where tabled (`Audit Success` 0x8020000000000000, `Audit Failure` 0x8010000000000000, `Classic` 0x80000000000000); undecodable bits kept as the hex string |
| `winlog.activity_id`, `winlog.related_activity_id` | keyword | `Correlation/@*` | omitted when empty |
| `winlog.process.pid` | long | `Execution/@ProcessID` | integer |
| `winlog.process.thread.id` | long | `Execution/@ThreadID` | integer |
| `winlog.user.identifier` | keyword | `Security/@UserID` | omitted when empty |
| `winlog.user.name`, `winlog.user.domain`, `winlog.user.type` | keyword | well-known SID table only | e.g. `S-1-5-18` → `SYSTEM`, `NT AUTHORITY`, `Well Known Group` |
| `winlog.event_data.<Name>` | keyword | `EventData/Data[@Name]` | every value as a string, see type policy |
| `winlog.event_data.paramN` | keyword | unnamed `EventData/Data` items, 1-based | Winlogbeat's convention |
| `winlog.event_data.Binary` | keyword | `EventData/Binary` | hex string as logged |
| `winlog.user_data.*` | object | `UserData/*` | provider XML as nested keys; attribute keys lose the `@` prefix, text nodes become the value |
| `ecs.version` | keyword | constant | `9.5.0` |

Winlogbeat's spec lists 129 named `winlog.event_data.*` fields; all are
`keyword`. Names not on that list are still emitted under
`winlog.event_data.*` and mapped dynamically as keyword, which is what
Winlogbeat's template does.

Dropped from the current layout: the `Event` wrapper, `@xmlns`, and the
`RawData` fallbacks, which become `paramN` and `Binary`.

### Routing

The module is chosen from the channel and provider, mirroring Winlogbeat's
routing pipeline: channel `Security` with provider
`Microsoft-Windows-Security-Auditing` → `security`; channel
`Microsoft-Windows-Sysmon/Operational` → `sysmon`; channels
`Microsoft-Windows-PowerShell/Operational` and `Windows PowerShell` →
`powershell`. Everything else is generic-only.

### Layer 2: Security module

Driven by an event ID table extracted from the pipeline at build time and
checked into `_ecs_tables.py`. Per event ID it holds `event.action`,
`event.category` (array), `event.type` (array), and for audit events
`event.outcome` from the keywords (`Audit Success` → `success`,
`Audit Failure` → `failure`). Coverage in Winlogbeat is 942 event IDs. Beyond
the table, a small set of field rules:

| Target | Type | Source `event_data` | Rule |
| --- | --- | --- | --- |
| `user.id`, `user.name`, `user.domain` | keyword | `SubjectUserSid`, `SubjectUserName`, `SubjectDomainName` | subject account; `-` and empty dropped |
| `user.target.id`, `user.target.name`, `user.target.domain` | keyword | `TargetUserSid`, `TargetUserName`, `TargetDomainName` | when the event has a target account |
| `user.effective.*` | keyword | 4624 `TargetUser*` when logon succeeded | Winlogbeat's convention for logons |
| `winlog.logon.id`, `winlog.logon.type` | keyword | `TargetLogonId`, `LogonType` | logon type number → name via the published table (`2` → `Interactive`, `3` → `Network`, `10` → `RemoteInteractive`, ...) |
| `winlog.logon.failure.reason`, `.status`, `.sub_status` | keyword | 4625 `FailureReason`, `Status`, `SubStatus` | status codes → text via the pipeline's table |
| `source.ip`, `source.port`, `source.domain` | ip, long, keyword | `IpAddress`, `IpPort`, `WorkstationName` | `IpAddress` of `-`, `::1`, `127.0.0.1` kept but `-` dropped; `::ffff:` IPv4-mapped prefix stripped; port `0` dropped |
| `process.pid`, `process.executable`, `process.name` | long, keyword, keyword | `ProcessId`/`NewProcessId`, `ProcessName`/`NewProcessName` | pids are hex strings (`0x1f4`) → int; name is the basename |
| `process.parent.pid`, `.executable`, `.name` | long, keyword | 4688 `ParentProcessId`, `ParentProcessName` | |
| `process.command_line` | wildcard | 4688 `CommandLine` | |
| `group.id`, `group.name`, `group.domain` | keyword | 4728-4762 `TargetSid`, `TargetUserName`, `TargetDomainName` | group management events |
| `service.name` | keyword | 4697 `ServiceName`; 5145 `ShareName` goes to `file.path` | |
| `file.path`, `file.name`, `file.extension` | keyword | 4663/5145 `ObjectName`, `RelativeTargetName` | |
| `related.user`, `related.ip` | keyword, ip arrays | union of the above | deduplicated |
| `winlog.computerObject.*`, `winlog.trust*` | keyword | 4741-4743, 4706-4707 | as published |

### Layer 2: Sysmon module

Sysmon writes typed data into `event_data` with stable names, so the
mapping is per event ID with far fewer tables. `event.category`, `event.type`
and `event.action` per ID (1 `Process Create` → `process` / `start`, 3
`Network connection` → `network` / `connection` `start` `protocol`, 7
`Image loaded` → `process` / `change`, 11 `File created` → `file` /
`creation`, 12-14 registry → `configuration` `registry` / `change`, 22 DNS
query → `network` / `protocol` `info`, and so on for all 29 IDs). Field
rules:

| Target | Type | Source | Rule |
| --- | --- | --- | --- |
| `process.pid`, `process.entity_id`, `process.executable`, `process.name`, `process.command_line`, `process.working_directory` | long, keyword, keyword, keyword, wildcard, keyword | `ProcessId`, `ProcessGuid`, `Image`, `CommandLine`, `CurrentDirectory` | `ProcessId` is decimal in Sysmon |
| `process.parent.*` | same | `ParentProcessId`, `ParentProcessGuid`, `ParentImage`, `ParentCommandLine` | |
| `process.hash.md5`, `.sha1`, `.sha256`, `.imphash` and `process.pe.*` | keyword | `Hashes` (`SHA256=...,MD5=...,IMPHASH=...`), `Company`, `Product`, `Description`, `FileVersion`, `OriginalFileName` | `Hashes` split on `,` then `=` |
| `file.path`, `file.name`, `file.directory`, `file.extension`, `file.hash.*` | keyword | `TargetFilename`, `ImageLoaded`, `Hashes` | for file and image-load events |
| `registry.path`, `registry.hive`, `registry.key`, `registry.value`, `registry.data.strings` | keyword | `TargetObject`, `Details` | hive from the path prefix (`HKLM`, `HKU`, ...) |
| `source.ip`, `source.port`, `destination.ip`, `destination.port`, `network.transport`, `network.protocol`, `network.direction`, `network.community_id` | ip, long, ip, long, keyword | `SourceIp`, `SourcePort`, `DestinationIp`, `DestinationPort`, `Protocol`, `Initiated`, `DestinationPortName` | Community ID reused from ParseZeekLogs; `Initiated` true → `egress`, false → `ingress` |
| `dns.question.name`, `dns.answers.*`, `dns.resolved_ip`, `sysmon.dns.status` | keyword, object, ip, keyword | 22 `QueryName`, `QueryResults`, `QueryStatus` | `QueryResults` is `type: N ...;` separated; IPs extracted, IPv4-mapped prefix stripped |
| `user.name`, `user.domain`, `user.id` | keyword | `User` (`DOMAIN\name`), `LogonId` | split on backslash |
| `sysmon.file.archived`, `sysmon.file.is_executable` | boolean | 23/26 `Archived`, `IsExecutable` | `true`/`false` strings |
| `rule.name` | keyword | `RuleName` | dropped when `-` |
| `related.hash`, `related.ip`, `related.user` | arrays | union | |

### Layer 2: PowerShell module

Events 400, 403, 600, 800 (`Windows PowerShell`) and 4103, 4104, 4105, 4106
(`Operational`). The classic-channel events carry `paramN` blobs of
`Key=Value` lines that must be parsed into `powershell.engine.*`,
`powershell.provider.*`, `powershell.command.*`, `powershell.runspace_id`,
`powershell.pipeline_id`, `powershell.id`, `powershell.sequence`,
`powershell.total`, `powershell.file.script_block_id`,
`powershell.file.script_block_text`, `powershell.connected_user.*` and
`process.command_line`, all typed as published (`sequence` and `total` are
`long`, `script_block_text` and `command.value` are `text`). 4103's
`Payload` contains `CommandInvocation(...)` and `ParameterBinding(...)`
lines that become the `powershell.command.invocation_details` array.

## Type policy

The published table decides the type; the transform coerces, and refuses to
emit a value that cannot be coerced rather than emitting a wrong type.

| Declared type | Accepted inputs | Rule |
| --- | --- | --- |
| `keyword`, `wildcard`, `text`, `match_only_text` | anything scalar | `str()`; booleans as `true`/`false`; `-` and empty strings dropped for ECS targets, kept for `winlog.event_data.*` |
| `long` | decimal strings, `0x`-prefixed hex strings, ints, floats with no fraction | hex is common for pids, handles, logon IDs and access masks in Security events; a value that is not an integer is dropped from the ECS target but stays in `winlog.event_data.*` |
| `ip` | IPv4/IPv6 text | validated with `ipaddress`; `::ffff:a.b.c.d` normalised to `a.b.c.d`; `-`, empty and `LOCAL` dropped |
| `boolean` | `true`/`false`, `True`/`False`, `1`/`0`, `yes`/`no` | anything else dropped |
| `date` | Windows `SystemTime`, Sysmon `UtcTime` (`2020-07-28 13:22:18.799`), epoch floats | emitted as ISO-8601 UTC; sub-microsecond ticks truncated because ES `date` stores milliseconds; `date_nanos` is not used because ECS declares `date` |
| array fields (per ECS `normalize: array`) | scalar or list | wrapped in a list; empty lists dropped |
| `object` / `flattened` | dict | `winlog.user_data` and `winlog.event_data` stay `object` with dynamic keyword children |

Two evtx-specific rules. `winlog.event_data.*` values are always strings
even when they look numeric, because the spec types them `keyword` and mixed
types across events would break the mapping; the typed ECS copy is where
numbers live. And a value that is `-` in a Security event means "not
applicable", so it never reaches an ECS field but stays in `event_data` for
fidelity with Winlogbeat, which keeps it too.

## Index mapping

`ensure_index(es, index, ecs=True)` creates the index from the same table:
declared ECS fields with their types, `winlog.*` per the published list,
`winlog.event_data` and `winlog.user_data` as objects with a dynamic template
mapping unknown children to `keyword`, `date_detection` and
`numeric_detection` off, plus a `keyword` multi-field on `wildcard` command
lines is left to the user's template rather than invented here. Deterministic
`_id` = `sha1(computer_name + channel + record_id)` so reloading a file is
idempotent; opt-out with `--no-dedupe`.

## Interfaces

- ECS is the default layout from 2.1. `--legacy` keeps the 2.0 document
  shape (`Event.System.*`, `Event.EventData.Data.*`) for existing dashboards;
  `--ecs-original` adds `event.original`; `--create-index` builds the mapping
  for whichever layout is in effect; `--no-dedupe` disables the deterministic
  `_id`.
- API: `to_ecs(xml, original=False) -> dict`, `ecs_index_body()`,
  `EvtxToElk(..., ecs=True)` with `ecs=False` selecting the legacy layout.
  `transform_event()` and `iter_documents()` keep producing the legacy shape
  so 2.0 callers are unaffected.
- Version 2.1.0. CHANGELOG calls out the default change and the `--legacy`
  flag.

## Testing

1. Unit tests per rule with hand-written `EventData`, including every type
   coercion case in the policy table.
2. Golden-file tests: Winlogbeat's published per-event-ID golden documents
   for the Security, Sysmon and PowerShell modules, compared field by field
   after removing the fields the offline path cannot produce (`message`,
   manifest-resolved names, `event.ingested`, `agent.*`, `host.*` beyond
   `name`). Any other difference is a failure.
3. Corpus invariants over all 37,364 EVTX-ATTACK-SAMPLES records: every
   emitted field's Python type matches its declared type, no `None` values,
   `@timestamp` present, `event.code` equals `winlog.event_id`, documents
   serialise, and the ECS index accepts them in the integration test with
   zero bulk failures.
4. Round-trip: a document indexed with the generated mapping can be queried
   as its types imply (CIDR on `source.ip`, range on `process.pid`).

## Phases

1. Generator script and tables; generic layer; type policy; `ecs_index_body`;
   `--ecs` on export and load; corpus invariant tests. This alone makes the
   data usable in an ECS deployment.
2. Security module: event ID table, logon and failure tables, the field
   rules above, golden tests.
3. Sysmon module.
4. PowerShell module, well-known SID and keyword tables.
5. Docs, CHANGELOG, README example with a Security 4624 document.

Phases 1 and 2 are roughly the size of the ParseZeekLogs ECS work; 3 and 4
together about the same again.

## Decisions

- `event.created` is kept and equals `@timestamp`, for dashboard
  friendliness.
- ECS is the default in 2.1, with `--legacy` for the 2.0 layout.
- `winlog.event_data.*` is always emitted, including for Sysmon events whose
  values are fully promoted to ECS fields, matching Winlogbeat.

## Implementation notes

Learned from comparing against Winlogbeat's golden documents, and applied:

- Hex values in `event_data` are rendered as minimal lowercase hex
  (`0x3e7`, not `0x00000000000003e7`) and GUIDs uppercase, as Winlogbeat's
  collector renders them. Empty strings are kept.
- Every `%%NNNN` message reference in Security `event_data` is resolved from
  the pipeline's description table, in place; multi-valued ones become lists.
- `winlog.user_data` is flattened to the children of the single root element
  plus `xml_name`, as Winlogbeat does; Security events that use `UserData`
  (1102) are handled by the same rules as `EventData` events.
- `event.action` for Sysmon and PowerShell is the provider's task name, which
  Winlogbeat renders from the manifest and which is fixed per event id; the
  Security task categories are tabled too.
- Sysmon `@timestamp` is `UtcTime`; `event.created` keeps `TimeCreated`.
- Sysmon command lines are split quote-aware; Security 4688 command lines on
  whitespace, with quotes kept, matching the two pipelines.
- All-zero hashes are dropped; imphash joins `related.hash`.
- `event.dataset` follows the Elastic Agent integrations
  (`system.security`, `windows.sysmon_operational`,
  `windows.powershell_operational`, `windows.powershell`,
  `windows.<channel>` otherwise); Winlogbeat's modules do not set it.
- Deliberate divergence: `winlog.event_data.*` is always kept, including
  for Sysmon where Winlogbeat removes promoted values.
