# evtxtoelk

Load Windows Event Log (`.evtx`) files into Elasticsearch, or export them as
JSON lines for any other collector.

[![PyPI](https://img.shields.io/pypi/v/evtxtoelk)](https://pypi.org/project/evtxtoelk/)
[![Build](https://github.com/dgunter/evtxtoelk/actions/workflows/build.yml/badge.svg)](https://github.com/dgunter/evtxtoelk/actions/workflows/build.yml)
[![Quality Gate](https://sonarcloud.io/api/project_badges/measure?project=dgunter_evtxtoelk&metric=alert_status)](https://sonarcloud.io/summary/new_code?id=dgunter_evtxtoelk)
[![Coverage](https://sonarcloud.io/api/project_badges/measure?project=dgunter_evtxtoelk&metric=coverage)](https://sonarcloud.io/summary/new_code?id=dgunter_evtxtoelk)

```bash
pip install evtxtoelk
evtxtoelk Security.evtx System.evtx http://localhost:9200 --create-index
```

Every record becomes one document with the event's `TimeCreated` as
`@timestamp`, the full `Event` structure, and `EventData` collapsed into a
searchable `{Name: value}` object. Corrupt records are skipped and counted
instead of aborting the load.

## Why this exists

EvtxToElk was written in 2018 on a threat hunt with a problem that will sound
familiar: a site with almost no internet bandwidth, laptops as the only
approved hardware, and five or six gigabytes of Windows Event Logs handed over
as `.evtx` files. Streaming tools were no use offline, and nobody was going to
read that much by hand. The fix was a small Python module that took the XML
[python-evtx](https://github.com/williballenthin/python-evtx) produces,
turned it into dictionaries with `xmltodict`, reshaped them to fit
Elasticsearch, and bulk-loaded them into a fresh ELK stack running on a
laptop. Kibana did the rest. Most of the code was the reshaping.

That module shipped as version 1.0 and was written up on the Dragos blog. The
post is gone from dragos.com, so it is preserved here as
[docs/blog-2018-evtxtoelk.md](docs/blog-2018-evtxtoelk.md), screenshots and
all, with notes on what changed. Version 2.0 is the same idea rebuilt for
Elasticsearch 8 and 9: a proper package with a command-line tool,
authentication and TLS options, a mapping that keeps field types stable, a
JSON-lines export, and a test suite that runs the loader over several hundred
real-world logs.

## What you get in Elasticsearch

A `Security.evtx` logon event comes out like this. Event IDs, users, logon
types and every other `EventData` value are individual fields you can filter,
aggregate and visualise in Kibana:

```json
{
  "@timestamp": "2016-07-08T18:12:51.681641+00:00",
  "Event": {
    "System": {
      "Provider": {"@Name": "Microsoft-Windows-Security-Auditing"},
      "EventID": {"@Qualifiers": "", "#text": "4624"},
      "TimeCreated": {"@SystemTime": "2016-07-08T18:12:51.681641+00:00"},
      "Channel": "Security",
      "Computer": "WKS01"
    },
    "EventData": {
      "Data": {"SubjectUserName": "alice", "LogonType": "2", "IpAddress": "10.0.0.7"}
    }
  },
  "meta": {"case": "1234"}
}
```

In Kibana, create a data view for `hostlogs*` with `@timestamp` as the time
field. A pie chart of `Event.System.EventID.#text` and a table of
`Event.EventData.Data.SubjectUserName` are the usual first two visualisations
on a new dataset; the original post walks through both.

## Install

Requires Python 3.10 or newer and Elasticsearch 8 or 9.

```bash
pip install evtxtoelk
```

or, from a checkout:

```bash
uv sync
```

## Usage

```
evtxtoelk FILE [FILE ...] DESTINATION [options]
```

`DESTINATION` is an Elasticsearch URL, a path ending in `.json`, `.jsonl` or
`.ndjson` to write JSON lines instead of indexing, or `-` for JSON lines on
stdout. A bare `host` or `host:port` is accepted and treated as
`http://host:9200`.

Load two logs into the default `hostlogs` index, creating it with the
recommended mapping if it does not exist:

```bash
evtxtoelk Security.evtx System.evtx http://localhost:9200 --create-index
```

Tag every document with case metadata and use a custom index:

```bash
evtxtoelk Security.evtx http://localhost:9200 -i case-1234 -m '{"case": "1234", "host": "WKS01"}'
```

Secured cluster with a self-signed certificate:

```bash
evtxtoelk Security.evtx https://es.example.com:9200 -u elastic --insecure
```

Or with a CA bundle and an API key:

```bash
evtxtoelk Security.evtx https://es.example.com:9200 --api-key "$ES_API_KEY" --ca-certs ca.pem
```

Export to a JSON-lines file instead (for Wazuh, Filebeat, jq, ...):

```bash
evtxtoelk Security.evtx security.json
```

Inspect the documents without touching anything:

```bash
evtxtoelk Security.evtx - | head -1 | jq .
```

### Options

| Option | Default | Purpose |
| --- | --- | --- |
| `-i`, `--index` | `hostlogs` | Target index |
| `-s`, `--bulk-size` | `500` | Documents per bulk request |
| `-m`, `--meta` | | JSON object stored under `meta` on every document |
| `-u`, `--user` / `-p`, `--password` | | Basic auth. Password is prompted when omitted |
| `--api-key` | | Elasticsearch API key |
| `--ca-certs` | | CA bundle for TLS verification |
| `-k`, `--insecure` | | Skip certificate verification |
| `--timeout` | `60` | Request timeout in seconds |
| `--create-index` | | Create the index with the recommended mapping |
| `-o`, `--output` | | Write JSON lines to a file (`-` for stdout) |
| `--dry-run` | | Same as `--output -` or a `-` destination |
| `-v`, `--verbose` | | Debug logging |

Exit status is `0` when every readable record was indexed and `1` when any
bulk item failed or the cluster could not be reached. Skipped (corrupt) records
are reported in the summary line but do not change the exit status.

### Document layout

```json
{
  "@timestamp": "2016-07-08T18:12:51.681641+00:00",
  "Event": {
    "System": {
      "Provider": {"@Name": "Microsoft-Windows-Security-Auditing"},
      "EventID": {"@Qualifiers": "", "#text": "4624"},
      "TimeCreated": {"@SystemTime": "2016-07-08T18:12:51.681641+00:00"},
      "Channel": "Security",
      "Computer": "WKS01"
    },
    "EventData": {
      "Data": {"SubjectUserName": "alice", "LogonType": "2"}
    }
  },
  "meta": {"case": "1234"}
}
```

Rules applied on the way in:

- `EventData/Data` elements with a `Name` become keys under `EventData.Data`.
  Dots in names are replaced with underscores and leading or trailing dots are
  dropped, because Elasticsearch rejects `.NETServiceMethod` style names.
- Unnamed `Data` elements and other odd payloads are serialised into a
  `RawData` string so a field never changes type between records.
- `--create-index` (or `scripts/apply_mapping.sh`) creates the index with
  `@timestamp` and `TimeCreated` mapped as dates and dynamic date and number
  detection turned off. Without it Elasticsearch dynamic mapping is used,
  which also works for the sample corpora but is more exposed to a stray
  value locking a field to the wrong type.

### Python API

```python
from evtxtoelk import EvtxToElk, ensure_index, iter_documents, make_client

es = make_client("https://es.example.com:9200", api_key="...", ca_certs="ca.pem")
ensure_index(es, "hostlogs")
result = EvtxToElk(es, index="hostlogs", metadata={"case": "1234"}).load("Security.evtx")
print(result.indexed, result.failed, result.skipped)

# or just iterate the documents
for doc in iter_documents("Security.evtx"):
    ...
```

The 1.x call `EvtxToElk.evtx_to_elk("Security.evtx", "localhost:9200")` still
works and returns a `LoadResult`.

## Development

```bash
uv sync                          # Python 3.14 environment with dev tools
uv run pytest                    # unit tests, no Elasticsearch needed
docker compose up -d --wait      # single-node Elasticsearch 9.5 on localhost:9200
uv run pytest -m integration     # end-to-end tests against it
docker compose down -v
```

To exercise the loader against a few hundred real-world logs, clone
[EVTX-ATTACK-SAMPLES](https://github.com/sbousseaden/EVTX-ATTACK-SAMPLES)
into `.cache/` and run the `samples` marker:

```bash
git clone --depth 1 https://github.com/sbousseaden/EVTX-ATTACK-SAMPLES .cache/EVTX-ATTACK-SAMPLES
uv run pytest -m samples
```

Lint and format with `uv run ruff check .` and `uv run ruff format .`.

### Releasing

Bump `version` in `pyproject.toml` and `__version__` in `evtxtoelk/__init__.py`,
note the release in `CHANGELOG.md`, merge, then publish a GitHub release whose
tag is `v<version>`. The Release workflow rebuilds, checks the tag against the
package version, and publishes to PyPI through trusted publishing.

CI runs the unit and integration tests on every push and pull request against
an Elasticsearch service container, then uploads coverage to
[SonarCloud](https://sonarcloud.io/project/overview?id=dgunter_evtxtoelk).

## Further reading

- [EvtxToElk: a Python module to load Windows Event Logs into Elasticsearch](docs/blog-2018-evtxtoelk.md), the July 2018 write-up by Dan Gunter and Marc Seitz, recovered from the [Wayback Machine](https://web.archive.org/web/20250812132436/https://www.dragos.com/blog/industry-news/evtxtoelk-a-python-module-to-load-windows-event-logs-into-elasticsearch/) after Dragos removed it.
- [CHANGELOG.md](CHANGELOG.md) for everything that changed in 2.0.
- Sample logs for trying it out: [EVTX-ATTACK-SAMPLES](https://github.com/sbousseaden/EVTX-ATTACK-SAMPLES) and the [python-evtx test data](https://github.com/williballenthin/python-evtx/tree/master/tests/data).

## Thanks

- [Willi Ballenthin](https://github.com/williballenthin) for python-evtx, which does the hard part.
- [@okynos](https://github.com/okynos) for the JSON file export.
- Marc Seitz, co-author of the original write-up.

## License

Apache License 2.0. See [LICENSE.txt](LICENSE.txt).
