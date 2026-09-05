"""Turn evtx records into Elasticsearch-friendly JSON documents.

The document layout is deliberately close to what earlier releases produced so
existing dashboards keep working:

* ``@timestamp`` at the root, ISO-8601 with a UTC offset.
* The full ``Event`` object as parsed from the record XML.
* ``Event.EventData.Data`` collapsed from a list of ``<Data Name=...>`` elements
  into a ``{Name: value}`` object so every field is searchable by name.
* Anything that cannot be expressed that way lands in a ``RawData`` string
  instead of producing a field whose type varies from record to record.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any

import xmltodict

log = logging.getLogger(__name__)

_SYSTEM_TIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%fZ",
    "%Y-%m-%dT%H:%M:%SZ",
)


def sanitize_key(name: str) -> str:
    """Make an EventData ``Name`` safe to use as an Elasticsearch field name.

    Elasticsearch rejects object fields that start or end with a dot
    (``.NETServiceMethod`` was the reported case) and treats interior dots as
    object separators, which produces mapping conflicts between records. Dots
    are replaced with underscores and an empty result falls back to ``_``.
    """
    cleaned = name.strip().strip(".").replace(".", "_")
    return cleaned or "_"


def parse_system_time(value: str) -> datetime:
    """Parse the ``TimeCreated/@SystemTime`` value into an aware UTC datetime.

    python-evtx renders it as ``2012-03-14 04:17:43.354563+00:00``; older
    versions omitted the offset and some records omit fractional seconds.
    """
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        parsed = None
        for fmt in _SYSTEM_TIME_FORMATS:
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        if parsed is None:
            raise
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _text_of(item: Any) -> Any:
    """Return the text payload of an xmltodict node (a dict with ``#text`` or a scalar)."""
    if isinstance(item, dict):
        return item.get("#text")
    return item


def _collapse_event_data(event: dict[str, Any]) -> None:
    """Rewrite ``event["EventData"]`` in place into a stable, searchable shape."""
    if "EventData" not in event:
        return
    event_data = event["EventData"]
    if event_data is None:
        del event["EventData"]
        return
    if not isinstance(event_data, dict):
        event["EventData"] = {"RawData": str(event_data)}
        return

    if "Data" not in event_data:
        return
    data = event_data.pop("Data")
    if data is None:
        return
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        event_data["RawData"] = str(data)
        return

    named: dict[str, Any] = {}
    unnamed: list[Any] = []
    for item in data:
        name = item.get("@Name") if isinstance(item, dict) else None
        if name:
            named[sanitize_key(str(name))] = _text_of(item)
        else:
            unnamed.append(_text_of(item))
    if named:
        event_data["Data"] = named
    if unnamed:
        event_data["RawData"] = json.dumps(unnamed)


def transform_event(xml: str, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    """Convert one record's XML into the document that gets indexed.

    Raises ``KeyError``/``ValueError`` when the XML lacks the ``System`` block
    or a parseable ``TimeCreated`` element; callers decide whether to skip.
    """
    doc = xmltodict.parse(xml)
    event = doc["Event"]
    system = event["System"]
    if not isinstance(system.get("TimeCreated"), dict):
        raise KeyError("TimeCreated")
    if not isinstance(system.get("EventID"), dict):
        # 2.0 consumers index Event.System.EventID.#text; keep that shape when the
        # parser omits an empty Qualifiers attribute.
        system["EventID"] = {"@Qualifiers": "", "#text": system.get("EventID")}
    when = parse_system_time(system["TimeCreated"]["@SystemTime"])
    stamp = when.isoformat()
    system["TimeCreated"]["@SystemTime"] = stamp
    doc["@timestamp"] = stamp
    _collapse_event_data(event)
    if metadata:
        doc["meta"] = dict(metadata)
    return doc


from evtxtoelk.parsers import ErrorHook, iter_record_xml  # noqa: E402  (re-exported)


def iter_documents(
    path: str,
    metadata: dict[str, Any] | None = None,
    on_error: ErrorHook | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield an indexable document for every readable record in ``path``."""
    for xml in iter_record_xml(path, on_error=on_error):
        try:
            yield transform_event(xml, metadata)
        except Exception as exc:  # noqa: BLE001 - one bad record must not stop the load
            log.warning("skipping record that could not be transformed: %s", exc)
            if on_error:
                on_error(-1, exc)
