"""Fidelity against Winlogbeat: parse its test .evtx files and compare with its golden documents.

Fields an offline .evtx cannot supply are excluded (see tests/data/winlogbeat/README.md).
Everything else must match, allowing for Winlogbeat representing single values as
scalars where ECS declares arrays and for IPv6 addresses in expanded form.
"""

import gzip
import ipaddress
import json
import pathlib
from datetime import datetime

import pytest

from evtxtoelk.ecs import to_ecs
from evtxtoelk.transform import iter_record_xml

ROOT = pathlib.Path(__file__).parent / "data" / "winlogbeat"
CASES = sorted(ROOT.glob("*/*.evtx.gz"))

#: Not derivable from an offline .evtx, or deliberately different (see docs/design-ecs.md).
IGNORED = (
    "message",  # rendered from message DLLs on the source host
    "winlog.opcode",  # manifest names; we table only the standard opcodes
    "winlog.keywords",  # manifest names beyond the standard bits
    "winlog.level",
    "winlog.user.",  # SID resolution
    "winlog.api",
    "winlog.event_original",
    "event.ingested",
    "event.created",  # Winlogbeat: read time; ours: event time, by decision
    "event.dataset",  # Winlogbeat modules do not set it; ours follows the Agent integrations
    "ecs.version",
    "agent.",
    "host.os.",
    "log.level",
    "@timestamp",  # sub-microsecond ticks and format; checked separately
    "winlog.time_created",
    "winlog.record_id",  # golden strips the field for some modules
    "winlog.version",
    "winlog.provider_guid",
    "dns.question.registered_domain",  # public suffix list
    "dns.question.top_level_domain",
    "dns.question.subdomain",
    "winlog.event_data.",  # kept in full, by decision; Winlogbeat removes promoted values
    "winlog.user_data.",
)


def _flat(doc, prefix=""):
    out = {}
    for key, value in doc.items():
        name = f"{prefix}{key}"
        if isinstance(value, dict):
            out.update(_flat(value, name + "."))
        else:
            out[name] = value
    return out


def _canonical(value):
    if isinstance(value, list) and len(value) == 1:
        value = value[0]
    if isinstance(value, list):
        return sorted(json.dumps(_canonical(v), sort_keys=True) for v in value)
    if isinstance(value, str):
        try:
            return str(ipaddress.ip_address(value))
        except ValueError:
            return value
    return value


def _load(evtx_gz: pathlib.Path, tmp_path: pathlib.Path):
    name = evtx_gz.name[: -len(".evtx.gz")]
    with gzip.open(evtx_gz.parent / f"{name}.golden.json.gz", "rt", encoding="utf-8") as fh:
        golden = json.load(fh)
    evtx = tmp_path / f"{name}.evtx"
    with gzip.open(evtx_gz, "rb") as fh:
        evtx.write_bytes(fh.read())
    ours = [to_ecs(xml) for xml in iter_record_xml(str(evtx))]
    return ours, golden


@pytest.mark.parametrize("evtx_gz", CASES, ids=lambda p: f"{p.parent.name}/{p.name[:-8]}")
def test_matches_winlogbeat_golden(evtx_gz, tmp_path):
    ours, golden = _load(evtx_gz, tmp_path)
    assert len(ours) == len(golden)
    problems = []
    for index, (mine, theirs) in enumerate(zip(ours, golden, strict=True)):
        flat_mine, flat_theirs = _flat(mine), _flat(theirs)
        # python-evtx rounds the 100 ns ticks to microseconds, which can roll the second over
        mine_ts = datetime.fromisoformat(mine["@timestamp"])
        theirs_ts = datetime.fromisoformat(theirs["@timestamp"].replace("Z", "+00:00"))
        assert abs((mine_ts - theirs_ts).total_seconds()) <= 1, index
        for key, expected in flat_theirs.items():
            if key.startswith(IGNORED):
                continue
            if key not in flat_mine:
                problems.append(f"[{index}] missing {key} = {expected!r}")
            elif _canonical(flat_mine[key]) != _canonical(expected):
                problems.append(f"[{index}] {key}: ours={flat_mine[key]!r} golden={expected!r}")
    assert not problems, "\n".join(problems[:40])
