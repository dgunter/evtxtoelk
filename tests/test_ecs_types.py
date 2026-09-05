"""The type policy: every coercion helper against a table of awkward inputs."""

from datetime import datetime, timezone

import pytest

from evtxtoelk import ecs
from evtxtoelk.ecs import unflatten

UTC = timezone.utc


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, 1), (False, 0), (42, 42), (7.0, 7), (7.5, None),
        ("12", 12), (" 0x1f ", 31), ("0X10", 16), ("abc", None), ("", None),
        (None, None), ([1], None), ({"a": 1}, None),
    ],
)  # fmt: skip
def test_as_int(value, expected):
    assert ecs._as_int(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [(True, 1.0), (3, 3.0), (2.5, 2.5), ("1.25", 1.25), ("x", None), (None, None), ([], None)],
)
def test_as_float(value, expected):
    assert ecs._as_float(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, True), (False, False), (1, True), (0, False), (2.0, True),
        ("true", True), ("True", True), ("yes", True), ("t", True), ("1", True),
        ("false", False), ("no", False), ("F", False), ("0", False),
        ("maybe", None), ("", None), (None, None), ([], None),
    ],
)  # fmt: skip
def test_as_bool(value, expected):
    assert ecs._as_bool(value) is expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("10.0.0.1", "10.0.0.1"), (" 10.0.0.1 ", "10.0.0.1"), ("::ffff:10.0.0.7", "10.0.0.7"),
        ("::FFFF:10.0.0.7", "10.0.0.7"), ("fe80::1", "fe80::1"), ("FE80:0:0:0:0:0:0:1", "fe80::1"),
        ("-", None), ("", None), ("LOCAL", None), ("not-an-ip", None), (None, None), (10, None),
    ],
)  # fmt: skip
def test_as_ip(value, expected):
    assert ecs._as_ip(value) == expected


def test_as_date():
    assert ecs._as_date(datetime(2020, 1, 1, tzinfo=UTC)) == "2020-01-01T00:00:00+00:00"
    assert ecs._as_date(0) == "1970-01-01T00:00:00+00:00"
    assert ecs._as_date(1.5) == "1970-01-01T00:00:01.500000+00:00"
    assert ecs._as_date("2019-04-27 15:57:53.368") == "2019-04-27T15:57:53.368000+00:00"
    assert ecs._as_date("2019-04-27T15:57:53.368Z") == "2019-04-27T15:57:53.368000+00:00"
    assert ecs._as_date("2019-04-27 15:57:53") == "2019-04-27T15:57:53+00:00"
    assert ecs._as_date("yesterday") is None
    assert ecs._as_date("") is None
    assert ecs._as_date(None) is None
    assert ecs._as_date(True) is None


def test_as_str():
    assert ecs._as_str(None) is None
    assert ecs._as_str(True) == "true"
    assert ecs._as_str(False) == "false"
    assert ecs._as_str(datetime(2020, 1, 1, tzinfo=UTC)) == "2020-01-01T00:00:00+00:00"
    assert ecs._as_str(5) == "5"
    assert ecs._as_str("x") == "x"


def test_normalize_scalar_and_braced_guid():
    assert ecs._normalize_scalar("0x000003E7") == "0x3e7"
    assert ecs._normalize_scalar("0x0") == "0x0"
    assert (
        ecs._normalize_scalar("{ad38ff07-bc05-4620-a79a-51e18f454768}")
        == "{AD38FF07-BC05-4620-A79A-51E18F454768}"
    )
    assert ecs._normalize_scalar("plain") == "plain"
    assert (
        ecs._braced_guid("ad38ff07-bc05-4620-a79a-51e18f454768")
        == "{AD38FF07-BC05-4620-A79A-51E18F454768}"
    )
    assert (
        ecs._braced_guid("{ad38ff07-bc05-4620-a79a-51e18f454768}")
        == "{AD38FF07-BC05-4620-A79A-51E18F454768}"
    )
    assert ecs._braced_guid("not a guid") == "not a guid"
    assert ecs._braced_guid("") is None
    assert ecs._braced_guid(None) is None


def test_elem_tolerates_empty_and_text_nodes():
    assert ecs._elem({"A": {"@x": "1"}}, "A") == {"@x": "1"}
    assert ecs._elem({"A": None}, "A") == {}
    assert ecs._elem({"A": "\n   "}, "A") == {}
    assert ecs._elem({}, "A") == {}


def test_append_semantics():
    flat = {}
    ecs._append(flat, "k", None)
    ecs._append(flat, "k", "")
    assert flat == {}
    ecs._append(flat, "k", "a")
    ecs._append(flat, "k", "a")
    ecs._append(flat, "k", "b")
    assert flat["k"] == ["a", "b"]
    flat = {"k": "scalar"}
    ecs._append(flat, "k", "scalar")
    assert flat["k"] == "scalar"
    ecs._append(flat, "k", "other")
    assert flat["k"] == ["scalar", "other"]


def test_unflatten_conflicts_both_orders():
    assert unflatten({"a": 1, "a.b": 2}) == {"a": {"value": 1, "b": 2}}
    assert unflatten({"a.b": 2, "a": 1}) == {"a": {"value": 1, "b": 2}}
    assert unflatten({"a.b.c": 1, "a.b": 0}) == {"a": {"b": {"value": 0, "c": 1}}}


def test_payload_item_variants():
    named, unnamed = {}, []
    ecs._payload_item("Binary", "0A", named, unnamed)
    ecs._payload_item("Binary", None, named, unnamed)
    ecs._payload_item("#text", "  \n", named, unnamed)
    ecs._payload_item("#text", "loose", named, unnamed)
    ecs._payload_item("Custom", "v", named, unnamed)
    ecs._payload_item("@Attr", "a", named, unnamed)
    ecs._payload_item("Nested", {"#text": "t", "@x": "1"}, named, unnamed)
    ecs._payload_item("Deep", {"Child": "x"}, named, unnamed)  # no #text: ignored
    assert named == {"Binary": "0A", "Custom": "v", "Attr": "a", "Nested": "t"}
    assert unnamed == ["loose"]


def test_event_data_fields_string_and_odd_inputs():
    assert ecs._event_data_fields("free text") == ({}, ["free text"])
    assert ecs._event_data_fields("") == ({}, [])
    assert ecs._event_data_fields("\n  ") == ({}, [])
    assert ecs._event_data_fields(None) == ({}, [])
    assert ecs._event_data_fields(["a"]) == ({}, [])
    named, unnamed = ecs._event_data_fields(
        {
            "Data": [
                {"@Name": "LogonGuid", "#text": "ad38ff07-bc05-4620-a79a-51e18f454768"},
                "x",
                {"#text": " "},
                None,
            ]
        }
    )
    assert named == {"LogonGuid": "{AD38FF07-BC05-4620-A79A-51E18F454768}"}
    assert unnamed == ["x", "", ""]


def test_strip_xml_lists_and_text():
    assert ecs._strip_xml([{"@a": "1"}, "s"]) == [{"a": "1"}, "s"]
    assert ecs._strip_xml({"#text": "only"}) == "only"
    assert ecs._strip_xml({"@xmlns": "ns", "K": {"#text": "v", "@a": "1"}}) == {
        "K": {"value": "v", "a": "1"}
    }
    assert ecs._strip_xml({"#text": "\n ", "K": "v"}) == {"K": "v"}
    assert ecs._strip_xml(5) == 5


def test_system_identity_with_bad_timestamp(caplog):
    flat = ecs._system_identity({"TimeCreated": {"@SystemTime": "garbage"}, "EventID": "1"})
    assert "@timestamp" not in flat
    assert flat["event.code"] == "1"
    assert "unparseable TimeCreated" in caplog.text


def test_decode_keywords():
    assert ecs._decode_keywords(0x8020000000000000) == ["Audit Success"]
    assert ecs._decode_keywords(0x8000000000000000) == []
    assert ecs._decode_keywords(0x8010000000000010) == ["Audit Failure", "0x10"]


def test_index_body_scalar_prefix_becomes_object(monkeypatch):
    monkeypatch.setattr(
        ecs,
        "FIELD_TYPES",
        {
            "a": "keyword",
            "a.b": "long",
            "f": "flattened",
            "f.x": "keyword",
            "n": "nested",
            "n.y": "keyword",
        },
    )
    props = ecs.ecs_index_body()["mappings"]["properties"]
    assert props["a"] == {"type": "object", "properties": {"b": {"type": "long"}}}
    assert props["f"] == {"type": "flattened"}
    assert props["n"]["type"] == "nested"
    assert props["n"]["properties"]["y"] == {"type": "keyword"}
