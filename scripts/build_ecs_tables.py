#!/usr/bin/env python3
"""Generate evtxtoelk/_ecs_tables.py from the published field references.

    uv run --with pyyaml scripts/build_ecs_tables.py

Sources, pinned by version:
  * ECS field reference, machine-readable release artifact
    https://raw.githubusercontent.com/elastic/ecs/v<ECS>/generated/ecs/ecs_flat.yml
  * Winlogbeat exported-fields documentation pages (winlog, security, sysmon,
    powershell, ecs) at https://www.elastic.co/docs/reference/beats/winlogbeat/

The output is a data-only module: field types, array fields, and the list of
winlog.event_data names the spec publishes. Transform logic lives in ecs.py.
"""

from __future__ import annotations

import html
import json
import pathlib
import re
import sys
import urllib.request

import yaml

ECS_VERSION = "9.5.0"
BEATS_VERSION = "9.5"
#: Exact Beats tag whose ingest pipelines supply the behaviour the references leave
#: undefined: event id -> category/type/action, and the code -> name lookup tables.
BEATS_TAG = "v9.5.3"
PIPELINES = {
    "security": f"https://raw.githubusercontent.com/elastic/beats/{BEATS_TAG}/x-pack/winlogbeat/module/security/ingest/security_standard.yml",
    "sysmon": f"https://raw.githubusercontent.com/elastic/beats/{BEATS_TAG}/x-pack/winlogbeat/module/sysmon/ingest/sysmon.yml",
}
ECS_URL = f"https://raw.githubusercontent.com/elastic/ecs/v{ECS_VERSION}/generated/ecs/ecs_flat.yml"
DOCS = "https://www.elastic.co/docs/reference/beats/winlogbeat"
PAGES = ["winlog", "security", "sysmon", "powershell", "ecs"]

_TYPE_MAP = {
    "keyword": "keyword",
    "text": "text",
    "match_only_text": "match_only_text",
    "wildcard": "wildcard",
    "constant_keyword": "keyword",
    "long": "long",
    "integer": "integer",
    "short": "integer",
    "byte": "integer",
    "double": "double",
    "float": "float",
    "scaled_float": "double",
    "half_float": "float",
    "boolean": "boolean",
    "ip": "ip",
    "date": "date",
    "flattened": "flattened",
    "object": "object",
    "geo_point": "geo_point",
    "nested": "nested",
    "array": "object",
}
_ENTRY = re.compile(r"<dt><strong><code>([^<]+)</code></strong></dt>\s*<dd>(.*?)</dd>", re.S)


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "evtxtoelk-build-ecs-tables"})
    with urllib.request.urlopen(req, timeout=120) as resp:  # noqa: S310 - pinned URLs
        return resp.read().decode("utf-8", errors="replace")


def parse_exported_fields(page_html: str) -> dict[str, str]:
    """``<dt><code>name</code></dt><dd>... type: keyword ...</dd>`` pairs -> {name: type}."""
    fields: dict[str, str] = {}
    for name, body in _ENTRY.findall(page_html):
        match = re.search(r"type:\s*([\w_]+)", html.unescape(body))
        fields[name.strip()] = _TYPE_MAP.get(match.group(1) if match else "object", "keyword")
    return fields


def _table_name(source: str, params: dict, index: int) -> str:
    """Name a script's params table after the event_data field it decodes."""
    first = next(iter(params))
    if str(first).isdigit() and isinstance(params[first], dict):
        return "events"
    reads = [
        r for r in re.findall(r"event_data\.(\w+)", source) if r not in ("put", "remove", "get")
    ]
    if reads:
        return reads[0]
    return f"table_{index}"


def parse_pipeline_tables(text: str) -> dict[str, dict]:
    """Every ``script.params`` map in an ingest pipeline, keyed by a descriptive name."""
    doc = yaml.safe_load(text)
    tables: dict[str, dict] = {}
    for index, proc in enumerate(doc.get("processors") or []):
        if not isinstance(proc, dict) or "script" not in proc:
            continue
        body = proc["script"] or {}
        params = body.get("params")
        if not isinstance(params, dict) or not params:
            continue
        name = _table_name(" ".join(str(body.get("source", "")).split()), params, index)
        while name in tables:
            name += "_"
        tables[name] = {str(k): v for k, v in params.items()}
    return tables


def main() -> int:
    print(f"fetching ECS {ECS_VERSION} ...", file=sys.stderr)
    ecs = yaml.safe_load(fetch(ECS_URL))
    ecs_types = {
        name: _TYPE_MAP.get(spec.get("type", "keyword"), "keyword") for name, spec in ecs.items()
    }
    ecs_arrays = {name for name, spec in ecs.items() if spec.get("normalize")}

    pages: dict[str, dict[str, str]] = {}
    for page in PAGES:
        print(f"fetching Winlogbeat exported fields: {page} ...", file=sys.stderr)
        pages[page] = parse_exported_fields(fetch(f"{DOCS}/exported-fields-{page}"))

    field_types: dict[str, str] = {}
    # ECS fields Winlogbeat can emit, typed by the ECS artifact (authoritative) when present.
    for name, ptype in pages["ecs"].items():
        field_types[name] = ecs_types.get(name, ptype)
    for page in ("winlog", "security", "sysmon", "powershell"):
        for name, ptype in pages[page].items():
            if name.startswith(("winlog.", "sysmon.", "powershell.")):
                field_types[name] = ptype
    field_types.setdefault("@timestamp", "date")
    field_types.setdefault("tags", "keyword")
    arrays = sorted(a for a in ecs_arrays if a in field_types)
    event_data_fields = sorted(
        n.split(".", 2)[2]
        for n in pages["winlog"]
        if n.startswith("winlog.event_data.") and n.count(".") == 2
    )

    pipeline_tables: dict[str, dict[str, dict]] = {}
    for module, url in PIPELINES.items():
        print(f"fetching Winlogbeat {module} pipeline tables ({BEATS_TAG}) ...", file=sys.stderr)
        pipeline_tables[module] = parse_pipeline_tables(fetch(url))

    out = pathlib.Path(__file__).resolve().parents[1] / "evtxtoelk" / "_ecs_tables.py"
    with out.open("w", encoding="utf-8") as fh:
        fh.write("# ruff: noqa: E501\n")
        fh.write('"""Generated by scripts/build_ecs_tables.py. Do not edit by hand.\n\n')
        fh.write(f"Field types from the ECS {ECS_VERSION} field reference and the Winlogbeat\n")
        fh.write(
            f"{BEATS_VERSION} exported-fields references (winlog, security, sysmon, powershell).\n"
        )
        fh.write('"""\n\n')
        fh.write(f"ECS_VERSION = {ECS_VERSION!r}\n")
        fh.write(f"BEATS_VERSION = {BEATS_VERSION!r}\n\n")
        fh.write("#: Elasticsearch field type per dotted field name.\n")
        fh.write(
            "FIELD_TYPES: dict[str, str] = "
            + json.dumps(dict(sorted(field_types.items())), indent=4)
            + "\n\n"
        )
        fh.write("#: ECS fields whose value is always an array.\n")
        fh.write(
            "ARRAY_FIELDS: frozenset[str] = frozenset(" + json.dumps(arrays, indent=4) + ")\n\n"
        )
        fh.write("#: winlog.event_data names the Winlogbeat reference publishes (all keyword).\n")
        fh.write(
            "EVENT_DATA_FIELDS: frozenset[str] = frozenset("
            + json.dumps(event_data_fields, indent=4)
            + ")\n"
        )
        fh.write(
            "\n#: Lookup tables from the Winlogbeat " + BEATS_TAG + " security and sysmon ingest\n"
        )
        fh.write(
            "#: pipelines: 'events' maps an event id to its category/type/action; the others\n"
        )
        fh.write("#: map a coded event_data value to its name.\n")
        fh.write(f"BEATS_TAG = {BEATS_TAG!r}\n")
        fh.write(
            "PIPELINE_TABLES: dict[str, dict[str, dict]] = "
            + json.dumps(pipeline_tables, indent=4)
            + "\n"
        )
    print(
        f"wrote {out}: {len(field_types)} field types, {len(arrays)} array fields, "
        f"{len(event_data_fields)} published event_data names",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
