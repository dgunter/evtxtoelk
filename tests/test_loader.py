from unittest import mock

import pytest

from evtxtoelk import loader
from evtxtoelk.loader import (
    INDEX_BODY,
    EvtxToElk,
    LoadResult,
    ensure_index,
    make_client,
    normalize_url,
)


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("localhost", "http://localhost:9200"),
        ("localhost:9200", "http://localhost:9200"),
        ("10.0.0.5:9201", "http://10.0.0.5:9201"),
        ("http://es.example.com", "http://es.example.com:9200"),
        ("https://es.example.com", "https://es.example.com"),
        ("https://es.example.com:9243/", "https://es.example.com:9243/"),
        ("http://user:pw@es.example.com", "http://user:pw@es.example.com:9200"),
        ("  localhost  ", "http://localhost:9200"),
    ],
)
def test_normalize_url(given, expected):
    assert normalize_url(given) == expected


def test_make_client_defaults():
    with mock.patch.object(loader, "Elasticsearch") as es_cls:
        make_client("localhost")
    es_cls.assert_called_once_with("http://localhost:9200", request_timeout=60)


def test_make_client_passes_auth_and_tls_options():
    with mock.patch.object(loader, "Elasticsearch") as es_cls:
        make_client(
            "https://es:9200",
            user="elastic",
            password="secret",
            api_key="abc==",
            ca_certs="/tmp/ca.pem",
            verify_certs=False,
            timeout=5,
        )
    es_cls.assert_called_once_with(
        "https://es:9200",
        request_timeout=5,
        basic_auth=("elastic", "secret"),
        api_key="abc==",
        ca_certs="/tmp/ca.pem",
        verify_certs=False,
        ssl_show_warn=False,
    )


def test_make_client_user_without_password_sends_empty_password():
    with mock.patch.object(loader, "Elasticsearch") as es_cls:
        make_client("localhost", user="elastic")
    assert es_cls.call_args.kwargs["basic_auth"] == ("elastic", "")


def test_ensure_index_creates_when_missing():
    es = mock.Mock()
    es.indices.exists.return_value = False
    assert ensure_index(es, "hostlogs", ecs=False) is True
    es.indices.create.assert_called_once_with(index="hostlogs", **INDEX_BODY)


def test_ensure_index_noop_when_present():
    es = mock.Mock()
    es.indices.exists.return_value = True
    assert ensure_index(es, "hostlogs") is False
    es.indices.create.assert_not_called()


def test_index_mapping_disables_dynamic_detection():
    mappings = INDEX_BODY["mappings"]
    assert mappings["date_detection"] is False
    assert mappings["numeric_detection"] is False
    assert mappings["properties"]["@timestamp"] == {"type": "date"}


def test_actions_wrap_documents_with_index_and_metadata(data_dir):
    es = mock.Mock()
    result = LoadResult()
    tool = EvtxToElk(es, index="idx", metadata={"case": "1"}, ecs=False)
    actions = list(tool.actions(str(data_dir / "issue_38.evtx"), result))
    assert len(actions) == 1
    assert actions[0]["_index"] == "idx"
    assert actions[0]["_source"]["meta"] == {"case": "1"}
    ecs_actions = list(
        EvtxToElk(es, index="idx", metadata={"observer.name": "x"}).actions(
            str(data_dir / "issue_38.evtx")
        )
    )
    assert ecs_actions[0]["_source"]["event"]["code"] == "4672"
    assert ecs_actions[0]["_source"]["observer"]["name"] == "x"
    assert len(ecs_actions[0]["_id"]) == 40
    assert (
        "_id"
        not in list(
            EvtxToElk(es, index="idx", dedupe=False).actions(str(data_dir / "issue_38.evtx"))
        )[0]
    )
    assert result.skipped == 0


def test_actions_count_skipped_records(data_dir):
    from evtxtoelk.parsers import PYTHON, select_backend

    expected = 4 if select_backend() == PYTHON else 0  # the Rust parser recovers every record
    result = LoadResult()
    list(EvtxToElk(mock.Mock()).actions(str(data_dir / "dns_log_malformed.evtx"), result))
    assert result.skipped == expected
    legacy = LoadResult()
    list(
        EvtxToElk(mock.Mock(), ecs=False).actions(str(data_dir / "dns_log_malformed.evtx"), legacy)
    )
    assert legacy.skipped == expected


def test_bulk_size_is_at_least_one():
    assert EvtxToElk(mock.Mock(), bulk_size=0).bulk_size == 1


def test_load_counts_successes_and_failures(data_dir):
    es = mock.Mock()

    def fake_streaming_bulk(client, actions, **kwargs):
        assert client is es
        assert kwargs["chunk_size"] == 7
        assert kwargs["raise_on_error"] is False
        for i, _action in enumerate(actions):
            if i % 2:
                yield False, {"index": {"error": {"reason": f"bad {i}"}}}
            else:
                yield True, {"index": {"_id": str(i)}}

    with mock.patch.object(loader.helpers, "streaming_bulk", fake_streaming_bulk):
        result = EvtxToElk(es, bulk_size=7, max_error_samples=3).load(
            str(data_dir / "issue_38.evtx")
        )
    assert (result.indexed, result.failed, result.skipped) == (1, 0, 0)
    assert result.ok

    with mock.patch.object(loader.helpers, "streaming_bulk", fake_streaming_bulk):
        result = EvtxToElk(es, bulk_size=7, max_error_samples=3).load(str(data_dir / "system.evtx"))
    assert result.indexed == 801
    assert result.failed == 800
    assert not result.ok
    assert len(result.errors) == 3
    assert "bad 1" in result.errors[0]


def test_load_many_sums_results(data_dir):
    es = mock.Mock()

    def all_ok(client, actions, **kwargs):
        for _ in actions:
            yield True, {}

    with mock.patch.object(loader.helpers, "streaming_bulk", all_ok):
        total = EvtxToElk(es).load_many(
            [str(data_dir / "issue_38.evtx"), str(data_dir / "dns_log_malformed.evtx")]
        )
    from evtxtoelk.parsers import PYTHON, select_backend

    if select_backend() == PYTHON:
        assert (total.indexed, total.failed, total.skipped) == (2, 0, 4)
    else:
        assert (total.indexed, total.failed, total.skipped) == (6, 0, 0)


def test_legacy_entry_point_builds_client_from_bare_host(data_dir):
    with (
        mock.patch.object(loader, "Elasticsearch") as es_cls,
        mock.patch.object(loader.helpers, "streaming_bulk") as bulk,
    ):
        bulk.return_value = iter([(True, {})])
        result = EvtxToElk.evtx_to_elk(
            str(data_dir / "issue_38.evtx"), "localhost:9200", elk_index="legacy", metadata={"a": 1}
        )
    es_cls.assert_called_once_with("http://localhost:9200", request_timeout=60)
    assert result.indexed == 1
    assert bulk.call_args.kwargs["chunk_size"] == 500


def test_ecs_actions_count_records_that_fail_to_map(monkeypatch, data_dir):
    from evtxtoelk import loader as loader_module
    from evtxtoelk.ecs import to_ecs as real

    calls = {"n": 0}

    def flaky(xml, original=False, meta=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("boom")
        return real(xml, original=original, meta=meta)

    monkeypatch.setattr("evtxtoelk.ecs.to_ecs", flaky)
    result = LoadResult()
    actions = list(EvtxToElk(mock.Mock()).actions(str(data_dir / "system.evtx"), result))
    assert result.skipped == 1
    assert len(actions) == 1600
    assert loader_module is not None


def test_ensure_index_ecs_mapping_has_field_limit():
    es = mock.Mock()
    es.indices.exists.return_value = False
    assert ensure_index(es, "ecs-idx") is True
    body = es.indices.create.call_args.kwargs
    assert body["settings"]["index.mapping.total_fields.limit"] >= 5000
    assert body["mappings"]["properties"]["source"]["properties"]["ip"] == {"type": "ip"}
