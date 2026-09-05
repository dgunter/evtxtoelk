import json
from datetime import datetime, timezone

import pytest

from evtxtoelk.transform import (
    iter_documents,
    iter_record_xml,
    parse_system_time,
    sanitize_key,
    transform_event,
)

NS = 'xmlns="http://schemas.microsoft.com/win/2004/08/events/event"'
SYSTEM = """
  <System>
    <Provider Name="Microsoft-Windows-Security-Auditing"
              Guid="{54849625-5478-4994-a5ba-3e3b0328c30d}"/>
    <EventID Qualifiers="">4624</EventID>
    <Level>0</Level>
    <TimeCreated SystemTime="2016-07-08 18:12:51.681641+00:00"/>
    <EventRecordID>42</EventRecordID>
    <Channel>Security</Channel>
    <Computer>WKS01</Computer>
  </System>
"""


def event(body: str = "") -> str:
    return f"<Event {NS}>{SYSTEM}{body}</Event>"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("SubjectUserName", "SubjectUserName"),
        (".NETServiceMethod", "NETServiceMethod"),
        ("Trailing.", "Trailing"),
        ("a.b.c", "a_b_c"),
        ("  padded ", "padded"),
        ("...", "_"),
        ("", "_"),
    ],
)
def test_sanitize_key(raw, expected):
    assert sanitize_key(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "2016-07-08 18:12:51.681641+00:00",
        "2016-07-08 18:12:51.681641",
        "2016-07-08 18:12:51",
        "2016-07-08T18:12:51.681641Z",
        "2016-07-08T18:12:51Z",
        "2016-07-08T18:12:51.681641000Z",
    ],
)
def test_parse_system_time_accepts_known_formats(raw):
    parsed = parse_system_time(raw)
    assert parsed.tzinfo is not None
    assert parsed.utcoffset().total_seconds() == 0
    assert parsed.replace(microsecond=0) == datetime(2016, 7, 8, 18, 12, 51, tzinfo=timezone.utc)


def test_parse_system_time_converts_offsets_to_utc():
    assert parse_system_time("2016-07-08 20:12:51+02:00").hour == 18


def test_parse_system_time_rejects_garbage():
    with pytest.raises(ValueError):
        parse_system_time("yesterday")


def test_transform_sets_timestamp_and_keeps_system_block():
    doc = transform_event(event())
    assert doc["@timestamp"] == "2016-07-08T18:12:51.681641+00:00"
    system = doc["Event"]["System"]
    assert system["TimeCreated"]["@SystemTime"] == doc["@timestamp"]
    assert system["EventID"] == {"@Qualifiers": "", "#text": "4624"}
    assert system["Computer"] == "WKS01"
    assert "EventData" not in doc["Event"]
    assert "meta" not in doc


def test_transform_attaches_metadata_copy():
    meta = {"case": "1234", "host": "WKS01"}
    doc = transform_event(event(), meta)
    assert doc["meta"] == meta
    assert doc["meta"] is not meta


def test_transform_collapses_named_data_list():
    doc = transform_event(
        event(
            "<EventData>"
            '<Data Name="SubjectUserName">alice</Data>'
            '<Data Name="LogonType">2</Data>'
            '<Data Name=".NETServiceMethod">Foo</Data>'
            '<Data Name="Empty"></Data>'
            "</EventData>"
        )
    )
    assert doc["Event"]["EventData"] == {
        "Data": {
            "SubjectUserName": "alice",
            "LogonType": "2",
            "NETServiceMethod": "Foo",
            "Empty": None,
        }
    }


def test_transform_single_named_data_element_is_still_a_mapping():
    doc = transform_event(event('<EventData><Data Name="Only">1</Data></EventData>'))
    assert doc["Event"]["EventData"] == {"Data": {"Only": "1"}}


def test_transform_unnamed_data_goes_to_rawdata():
    doc = transform_event(event("<EventData><Data>first</Data><Data>second</Data></EventData>"))
    event_data = doc["Event"]["EventData"]
    assert "Data" not in event_data
    assert json.loads(event_data["RawData"]) == ["first", "second"]


def test_transform_mixed_named_and_unnamed_data():
    doc = transform_event(event('<EventData><Data Name="A">1</Data><Data>loose</Data></EventData>'))
    event_data = doc["Event"]["EventData"]
    assert event_data["Data"] == {"A": "1"}
    assert json.loads(event_data["RawData"]) == ["loose"]


def test_transform_text_only_data_goes_to_rawdata():
    doc = transform_event(event("<EventData><Data>just text</Data></EventData>"))
    assert doc["Event"]["EventData"] == {"RawData": "just text"}


def test_transform_keeps_sibling_eventdata_children():
    doc = transform_event(
        event('<EventData><Data Name="param1">svc</Data><Binary>AAEC</Binary></EventData>')
    )
    assert doc["Event"]["EventData"] == {"Binary": "AAEC", "Data": {"param1": "svc"}}


def test_transform_empty_eventdata_is_removed():
    doc = transform_event(event("<EventData/>"))
    assert "EventData" not in doc["Event"]


def test_transform_empty_data_element_is_removed():
    doc = transform_event(event("<EventData><Data/></EventData>"))
    assert doc["Event"]["EventData"] == {}


def test_transform_text_eventdata_becomes_rawdata():
    doc = transform_event(event("<EventData>free text</EventData>"))
    assert doc["Event"]["EventData"] == {"RawData": "free text"}


def test_transform_keeps_userdata_untouched():
    doc = transform_event(
        event("<UserData><AutoBackup><Channel>System</Channel></AutoBackup></UserData>")
    )
    assert doc["Event"]["UserData"] == {"AutoBackup": {"Channel": "System"}}


def test_transform_requires_system_block():
    with pytest.raises(KeyError):
        transform_event(f"<Event {NS}><EventData/></Event>")


@pytest.mark.parametrize(
    ("name", "count"),
    [("system.evtx", 1601), ("security.evtx", 2261), ("issue_38.evtx", 1)],
)
def test_iter_documents_reads_every_record(data_dir, name, count):
    docs = list(iter_documents(str(data_dir / name)))
    assert len(docs) == count
    for doc in docs:
        assert doc["@timestamp"].endswith("+00:00")
        assert "System" in doc["Event"]
        event_data = doc["Event"].get("EventData")
        if event_data is not None and "Data" in event_data:
            assert isinstance(event_data["Data"], dict)
            assert all(isinstance(k, str) and k for k in event_data["Data"])


def test_iter_documents_skips_corrupt_records_and_reports_them(data_dir):
    errors = []
    docs = list(
        iter_documents(
            str(data_dir / "dns_log_malformed.evtx"), on_error=lambda _o, exc: errors.append(exc)
        )
    )
    # python-evtx recovers one record from this file and fails on the rest.
    assert len(docs) == 1
    assert len(errors) == 4
    assert all(isinstance(e, UnicodeDecodeError) for e in errors)
    assert docs[0]["Event"]["EventData"]["Data"]["QNAME"].endswith("windows.net.")


def test_iter_record_xml_yields_xml_strings(data_dir):
    first = next(iter_record_xml(str(data_dir / "issue_38.evtx")))
    assert first.lstrip().startswith("<Event")


def test_iter_documents_skips_records_that_fail_to_transform(monkeypatch, data_dir):
    import evtxtoelk.transform as t

    calls = {"n": 0}
    real = t.transform_event

    def flaky(xml, metadata=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("boom")
        return real(xml, metadata)

    monkeypatch.setattr(t, "transform_event", flaky)
    seen = []
    docs = list(
        iter_documents(str(data_dir / "issue_38.evtx"), on_error=lambda o, e: seen.append(o))
    )
    assert docs == []
    assert seen == [-1]


def test_iter_record_xml_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        list(iter_record_xml(str(tmp_path / "nope.evtx")))


def test_iter_record_xml_skips_unreadable_chunks(monkeypatch, data_dir):
    """A chunk whose record table cannot be read is reported and skipped, not fatal."""
    import evtxtoelk.transform as t

    class BadChunk:
        def offset(self):
            return 4096

        def records(self):
            raise RuntimeError("corrupt chunk")

    class FakeEvtx:
        def __init__(self, path):
            self.path = path

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def chunks(self):
            yield BadChunk()

    monkeypatch.setattr(t, "Evtx", FakeEvtx)
    seen = []
    assert list(iter_record_xml("ignored.evtx", on_error=lambda o, e: seen.append(o))) == []
    assert seen == [4096]


def test_collapse_handles_non_string_scalar_data():
    """Defensive branch: a Data payload that is neither list, dict, str nor None."""
    from evtxtoelk.transform import _collapse_event_data

    ev = {"EventData": {"Data": 42}}
    _collapse_event_data(ev)
    assert ev["EventData"] == {"RawData": "42"}
