"""Map Windows event records to Elastic Common Schema documents.

The layout follows Winlogbeat: the ``System`` block becomes ``winlog.*`` plus
the ECS ``event.*``, ``host.name`` and ``log.level`` fields; ``EventData``
becomes ``winlog.event_data.*`` (always strings, as the Winlogbeat reference
types them); ``UserData`` becomes ``winlog.user_data``. Field types come from
:mod:`evtxtoelk._ecs_tables`, generated from the published ECS |ECS_VERSION|
and Winlogbeat |BEATS_VERSION| references, and every emitted value is coerced
to its declared type. :func:`ecs_index_body` builds a matching mapping.

Not available from an offline ``.evtx``: the rendered ``message``, keyword,
opcode and task *names* for providers other than the standard ones, and
account names for SIDs beyond the well-known ones. See docs/design-ecs.md.
"""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import re
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

import xmltodict

from evtxtoelk._ecs_tables import ARRAY_FIELDS, BEATS_VERSION, ECS_VERSION, FIELD_TYPES
from evtxtoelk.transform import parse_system_time

__all__ = [
    "BEATS_VERSION",
    "ECS_VERSION",
    "document_id",
    "ecs_index_body",
    "to_ecs",
    "unflatten",
]

log = logging.getLogger(__name__)
Flat = dict[str, Any]

# ECS field names used repeatedly.
TIMESTAMP = "@timestamp"
EVENT_DATA = "winlog.event_data"
USER_DATA = "winlog.user_data"

_LEVELS = {
    0: "information",
    1: "critical",
    2: "error",
    3: "warning",
    4: "information",
    5: "verbose",
}
_OPCODES = {
    0: "Info", 1: "Start", 2: "Stop", 3: "DC Start", 4: "DC Stop", 5: "Extension",
    6: "Reply", 7: "Resume", 8: "Suspend", 9: "Send", 240: "Receive",
}  # fmt: skip
#: Standard keyword bits (winmeta), the only ones decodable without a provider manifest.
_KEYWORDS = {
    0x0001000000000000: "Response Time",
    0x0002000000000000: "WDI Diag",
    0x0004000000000000: "WDI Context",
    0x0008000000000000: "SQM",
    0x0010000000000000: "Audit Failure",
    0x0020000000000000: "Audit Success",
    0x0040000000000000: "Correlation Hint",
    0x0080000000000000: "Classic",
}
_KEYWORD_RESERVED = 0x8000000000000000  # set on every event; carries no meaning
#: Well-known SIDs: (name, domain, type).
_WELL_KNOWN_SIDS = {
    "S-1-0-0": ("Nobody", "", "Well Known Group"),
    "S-1-1-0": ("Everyone", "", "Well Known Group"),
    "S-1-5-7": ("ANONYMOUS LOGON", "NT AUTHORITY", "Well Known Group"),
    "S-1-5-18": ("SYSTEM", "NT AUTHORITY", "Well Known Group"),
    "S-1-5-19": ("LOCAL SERVICE", "NT AUTHORITY", "Well Known Group"),
    "S-1-5-20": ("NETWORK SERVICE", "NT AUTHORITY", "Well Known Group"),
    "S-1-5-32-544": ("Administrators", "BUILTIN", "Alias"),
    "S-1-5-32-545": ("Users", "BUILTIN", "Alias"),
    "S-1-5-32-546": ("Guests", "BUILTIN", "Alias"),
}
#: Channel/provider -> (event.module, event.dataset). Dataset names follow the
#: Elastic Agent Windows and System integrations.
_ROUTES: list[tuple[str, str | None, str, str]] = [
    ("Security", None, "security", "system.security"),
    ("Microsoft-Windows-Sysmon/Operational", None, "sysmon", "windows.sysmon_operational"),
    (
        "Microsoft-Windows-PowerShell/Operational",
        None,
        "powershell",
        "windows.powershell_operational",
    ),
    ("Windows PowerShell", None, "powershell", "windows.powershell"),
]


# -- value helpers ------------------------------------------------------------------------


def _text(node: Any) -> str | None:
    """xmltodict leaf -> string (``#text`` of an element, or the scalar itself)."""
    if node is None:
        return None
    if isinstance(node, dict):
        node = node.get("#text")
    return None if node is None else str(node)


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        text = value.strip()
        try:
            return int(text, 16) if text.lower().startswith("0x") else int(text)
        except ValueError:
            return None
    return None


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("true", "1", "yes", "t"):
            return True
        if text in ("false", "0", "no", "f"):
            return False
    return None


def _as_ip(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text in ("", "-", "LOCAL"):
        return None
    if text.lower().startswith("::ffff:") and text.count(".") == 3:
        text = text[7:]
    try:
        return str(ipaddress.ip_address(text))
    except ValueError:
        return None


_HEX = re.compile(r"^0[xX][0-9A-Fa-f]+$")
_GUID = re.compile(
    r"^\{[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\}$"
)


def _normalize_scalar(value: str) -> str:
    """Render values the way Winlogbeat does: minimal lowercase hex, uppercase GUIDs."""
    if _HEX.match(value):
        return f"0x{int(value, 16):x}"
    if _GUID.match(value):
        return value.upper()
    return value


_SYSMON_TIME = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(\.\d+)?$")


def _as_date(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
    if isinstance(value, str) and value.strip():
        text = value.strip()
        if _SYSMON_TIME.match(text):
            text = text.replace(" ", "T") + "+00:00"
        try:
            return parse_system_time(text).isoformat()
        except ValueError:
            return None
    return None


def _as_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


_COERCE: dict[str, Callable[[Any], Any]] = {
    "long": _as_int,
    "integer": _as_int,
    "double": _as_float,
    "float": _as_float,
    "boolean": _as_bool,
    "ip": _as_ip,
    "date": _as_date,
    "keyword": _as_str,
    "text": _as_str,
    "wildcard": _as_str,
    "match_only_text": _as_str,
}


def _has_objects(value: Any) -> bool:
    return isinstance(value, dict) or (
        isinstance(value, list) and bool(value) and isinstance(value[0], dict)
    )


def _coerce_value(key: str, value: Any) -> Any:
    """Coerce one value to its declared type. ``None`` means "drop the field"."""
    if key.startswith((EVENT_DATA + ".", USER_DATA + ".")) or key == USER_DATA:
        return value  # already shaped by the generic layer; empty strings are kept
    convert = _COERCE.get(FIELD_TYPES.get(key, ""))
    if convert is not None and not _has_objects(value):
        if isinstance(value, list):
            value = [c for c in (convert(v) for v in value) if c is not None]
        else:
            value = convert(value)
    if key in ARRAY_FIELDS and value is not None and not isinstance(value, list):
        value = [value]
    if value is None or value == [] or value == "":
        return None
    return value


def _coerce_types(flat: Flat) -> None:
    coerced = {key: _coerce_value(key, value) for key, value in flat.items()}
    flat.clear()
    flat.update({key: value for key, value in coerced.items() if value is not None})


def _append(flat: Flat, key: str, value: Any) -> None:
    if value is None or value == "":
        return
    current = flat.get(key)
    if current is None:
        flat[key] = [value]
    elif isinstance(current, list):
        if value not in current:
            current.append(value)
    elif current != value:
        flat[key] = [current, value]


def unflatten(flat: Flat) -> dict[str, Any]:
    """``{"a.b": 1}`` -> ``{"a": {"b": 1}}``; a scalar that is also a prefix goes to ``.value``."""
    out: dict[str, Any] = {}
    for key in sorted(flat, key=lambda k: (k.count("."), k)):
        parts = key.split(".")
        node = out
        for part in parts[:-1]:
            child = node.get(part)
            if not isinstance(child, dict):
                child = {} if child is None else {"value": child}
                node[part] = child
            node = child
        leaf = parts[-1]
        if isinstance(node.get(leaf), dict):
            node[leaf]["value"] = flat[key]
        else:
            node[leaf] = flat[key]
    return out


# -- generic layer ------------------------------------------------------------------------


def _decode_keywords(mask: int) -> list[str]:
    names = []
    remaining = mask & ~_KEYWORD_RESERVED
    for bit, name in _KEYWORDS.items():
        if remaining & bit:
            names.append(name)
            remaining &= ~bit
    if remaining:
        names.append(f"0x{remaining:x}")
    return names


def _event_data_fields(event_data: Any) -> tuple[dict[str, str], list[str]]:
    """``EventData`` -> ({Name: value}, [unnamed values]). Values are strings, ``-`` kept."""
    named: dict[str, str] = {}
    unnamed: list[str] = []
    if event_data is None or isinstance(event_data, str):
        if isinstance(event_data, str) and event_data:
            unnamed.append(event_data)
        return named, unnamed
    if not isinstance(event_data, dict):
        return named, unnamed
    for key, value in event_data.items():
        if key == "#text" and not str(value).strip():
            continue  # whitespace between elements
        if key == "Data":
            items = value if isinstance(value, list) else [value]
            for item in items:
                if isinstance(item, dict) and item.get("@Name"):
                    named[str(item["@Name"])] = _normalize_scalar(_text(item) or "")
                else:
                    text = _text(item)
                    if text is not None:
                        unnamed.append(text)
        elif key == "Binary":
            text = _text(value)
            if text:
                named["Binary"] = text
        else:
            # Provider-defined children (e.g. Sysmon writes <Data> only, but be safe).
            text = _text(value) if not isinstance(value, dict) or "#text" in value else None
            if text is not None:
                named[key.lstrip("@")] = text
    return named, unnamed


def _strip_xml(node: Any) -> Any:
    """xmltodict node -> plain dict/str with ``@`` attribute prefixes and ``#text`` removed."""
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for key, value in node.items():
            if key.startswith("@xmlns"):
                continue
            if key == "#text" and len(node) > 1 and not str(value).strip():
                continue  # whitespace between child elements
            name = "value" if key == "#text" else key.lstrip("@")
            out[name] = _strip_xml(value)
        if list(out) == ["value"]:
            return out["value"]
        return out
    if isinstance(node, list):
        return [_strip_xml(v) for v in node]
    return node


def _generic(event: dict[str, Any], original: str | None) -> Flat:
    system = event.get("System") or {}
    flat: Flat = {}
    provider = system.get("Provider") or {}
    event_id = _text(system.get("EventID"))
    stamp = None
    time_created = (system.get("TimeCreated") or {}).get("@SystemTime")
    if time_created:
        try:
            stamp = parse_system_time(time_created).isoformat()
        except ValueError:
            log.warning("unparseable TimeCreated %r", time_created)
    if stamp:
        flat[TIMESTAMP] = stamp
        flat["event.created"] = stamp
        flat["winlog.time_created"] = stamp
    flat["event.kind"] = "event"
    flat["event.code"] = event_id
    flat["event.provider"] = provider.get("@Name")
    flat["winlog.provider_name"] = provider.get("@Name")
    flat["winlog.provider_guid"] = _normalize_scalar(provider.get("@Guid") or "") or None
    flat["winlog.event_id"] = event_id
    flat["winlog.channel"] = _text(system.get("Channel"))
    flat["winlog.computer_name"] = _text(system.get("Computer"))
    flat["host.name"] = _text(system.get("Computer"))
    flat["winlog.record_id"] = _text(system.get("EventRecordID"))
    flat["winlog.version"] = _text(system.get("Version"))
    level = _as_int(_text(system.get("Level")))
    if level is not None:
        flat["log.level"] = _LEVELS.get(level, str(level))
    opcode = _as_int(_text(system.get("Opcode")))
    if opcode is not None:
        flat["winlog.opcode"] = _OPCODES.get(opcode, str(opcode))
    task = _text(system.get("Task"))
    if task not in (None, ""):
        flat["winlog.task"] = task
    keywords = _as_int(_text(system.get("Keywords")))
    if keywords is not None:
        flat["winlog.keywords"] = _decode_keywords(keywords)
    correlation = system.get("Correlation") or {}
    flat["winlog.activity_id"] = _normalize_scalar(correlation.get("@ActivityID") or "") or None
    flat["winlog.related_activity_id"] = (
        _normalize_scalar(correlation.get("@RelatedActivityID") or "") or None
    )
    execution = system.get("Execution") or {}
    flat["winlog.process.pid"] = execution.get("@ProcessID")
    flat["winlog.process.thread.id"] = execution.get("@ThreadID")
    sid = (system.get("Security") or {}).get("@UserID") or None
    if sid:
        flat["winlog.user.identifier"] = sid
        known = _WELL_KNOWN_SIDS.get(sid)
        if known:
            flat["winlog.user.name"], flat["winlog.user.domain"], flat["winlog.user.type"] = known

    named, unnamed = _event_data_fields(event.get("EventData"))
    for name, value in named.items():
        flat[f"{EVENT_DATA}.{name}"] = value
    for index, value in enumerate(unnamed, 1):
        flat[f"{EVENT_DATA}.param{index}"] = value
    user_data = event.get("UserData")
    if isinstance(user_data, dict):
        stripped = _strip_xml(user_data)
        if isinstance(stripped, dict) and stripped:
            if len(stripped) == 1 and isinstance(next(iter(stripped.values())), dict):
                ((root_name, children),) = stripped.items()
                stripped = {"xml_name": root_name, **children}
            for name, value in stripped.items():
                if isinstance(value, str):
                    value = _normalize_scalar(value)
                flat[f"{USER_DATA}.{name}"] = value

    channel, prov = flat.get("winlog.channel"), flat.get("winlog.provider_name")
    module, dataset = None, None
    for route_channel, route_provider, route_module, route_dataset in _ROUTES:
        if channel == route_channel and (route_provider is None or prov == route_provider):
            module, dataset = route_module, route_dataset
            break
    if module:
        flat["event.module"] = module
    flat["event.dataset"] = dataset or f"windows.{_slug(channel or prov or 'unknown')}"
    if original is not None:
        flat["event.original"] = original
    flat["ecs.version"] = ECS_VERSION
    return {k: v for k, v in flat.items() if v is not None}


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


# -- public API --------------------------------------------------------------------------


def to_ecs(
    record: str | dict[str, Any],
    *,
    original: bool = False,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the ECS document for one record (its XML text, or the xmltodict-parsed dict)."""
    if isinstance(record, str):
        # Keep leading/trailing whitespace in values: Winlogbeat does, and command
        # lines are compared byte for byte by detection rules.
        parsed = xmltodict.parse(record, strip_whitespace=False)
        raw_xml = record if original else None
    else:
        parsed = record
        raw_xml = None
    event = parsed.get("Event") if isinstance(parsed, dict) else None
    if not isinstance(event, dict):
        raise ValueError("record has no <Event> element")
    flat = _generic(event, raw_xml)
    module = flat.get("event.module")
    if module:
        from evtxtoelk.ecs_modules import MODULES

        rules = MODULES.get(module)
        if rules is not None:
            rules(flat)
    if meta:
        flat.update(meta)
    _coerce_types(flat)
    return unflatten(flat)


def document_id(doc: dict[str, Any]) -> str | None:
    """Deterministic id from computer name, channel and record id, or None if any is missing."""
    winlog = doc.get("winlog") or {}
    parts = (winlog.get("computer_name"), winlog.get("channel"), winlog.get("record_id"))
    if not all(parts):
        return None
    return hashlib.sha1("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()  # noqa: S324


_CONTAINER_TYPES = {"object", "nested", "flattened"}
_TEXT_TYPES = {"text", "match_only_text", "wildcard", "keyword"}


def _multi_fields(types: dict[str, str]) -> dict[str, dict[str, str]]:
    """Multi-fields such as ``user.name.text`` -> ``{"user.name": {"text": "match_only_text"}}``."""
    out: dict[str, dict[str, str]] = {}
    for key, ftype in types.items():
        parent, _, sub = key.rpartition(".")
        if parent in types and types[parent] not in _CONTAINER_TYPES and ftype in _TEXT_TYPES:
            out.setdefault(parent, {})[sub] = ftype
    return out


def ecs_index_body() -> dict[str, Any]:
    """Elasticsearch index body whose mapping mirrors :data:`FIELD_TYPES`."""
    types = dict(FIELD_TYPES)
    types[EVENT_DATA] = "object"
    types[USER_DATA] = "object"
    multi = _multi_fields(types)
    for parent, subs in multi.items():
        for sub in subs:
            types.pop(f"{parent}.{sub}", None)
    prefixes = {k.rsplit(".", 1)[0] for k in types if "." in k}
    root: dict[str, Any] = {}
    for key in sorted(types, key=lambda k: (k.count("."), k)):
        ftype = types[key]
        parts = key.split(".")
        node = root
        for part in parts[:-1]:
            node = node.setdefault(part, {"type": "object", "properties": {}})
            node.setdefault("properties", {})
            node = node["properties"]
        leaf = parts[-1]
        if key in prefixes and ftype not in _CONTAINER_TYPES:
            node.setdefault(leaf, {"type": "object", "properties": {}})
        elif ftype == "flattened":
            node[leaf] = {"type": "flattened"}
        elif ftype in _CONTAINER_TYPES:
            node.setdefault(leaf, {"type": ftype, "properties": {}})
        elif key in multi:
            node[leaf] = {"type": ftype, "fields": {n: {"type": t} for n, t in multi[key].items()}}
        else:
            node[leaf] = {"type": ftype}
    return {
        # 1,500+ declared fields plus dynamic event_data keys: raise the default 1,000 limit,
        # as Winlogbeat's own index template does.
        "settings": {"index.mapping.total_fields.limit": 10000},
        "mappings": {
            "dynamic": True,
            "date_detection": False,
            "numeric_detection": False,
            "dynamic_templates": [
                {
                    "winlog_data_as_keyword": {
                        "path_match": "winlog.*_data.*",
                        "match_mapping_type": "*",
                        "mapping": {"type": "keyword", "ignore_above": 8191},
                    }
                }
            ],
            "properties": root,
        },
    }
