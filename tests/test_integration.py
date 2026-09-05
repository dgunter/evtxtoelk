"""End-to-end tests against a live Elasticsearch (``docker compose up -d``).

Run with ``pytest -m integration``. Skipped when nothing answers at
EVTXTOELK_ES_URL unless EVTXTOELK_REQUIRE_ES=1 (CI sets that).
"""

import uuid

import pytest
from elasticsearch import Elasticsearch

from evtxtoelk import EvtxToElk, ensure_index, make_client
from evtxtoelk.cli import main

pytestmark = pytest.mark.integration


@pytest.fixture
def es(es_url):
    client = make_client(es_url)
    yield client
    client.close()


@pytest.fixture
def index(es):
    name = f"evtxtoelk-test-{uuid.uuid4().hex[:8]}"
    yield name
    es.indices.delete(index=name, ignore_unavailable=True)


def _count(es: Elasticsearch, index: str) -> int:
    es.indices.refresh(index=index)
    return es.count(index=index)["count"]


def test_server_is_current_major(es):
    major = int(es.info()["version"]["number"].split(".")[0])
    assert major >= 8, "tests target the _type-less bulk API of Elasticsearch 8+"


def test_load_system_log_with_created_index(es, index, data_dir):
    assert ensure_index(es, index, ecs=False) is True
    assert ensure_index(es, index, ecs=False) is False

    result = EvtxToElk(es, index=index, bulk_size=200, metadata={"case": "it"}, ecs=False).load(
        str(data_dir / "system.evtx")
    )
    assert result.ok, result.errors
    assert result.indexed == 1601
    assert result.skipped == 0
    assert _count(es, index) == 1601

    mapping = es.indices.get_mapping(index=index)[index]["mappings"]
    assert mapping["properties"]["@timestamp"]["type"] == "date"
    time_created = mapping["properties"]["Event"]["properties"]["System"]["properties"][
        "TimeCreated"
    ]["properties"]["@SystemTime"]
    assert time_created["type"] == "date"

    hits = es.search(
        index=index,
        query={"term": {"Event.System.EventID.#text.keyword": "7036"}},
        size=1,
    )["hits"]
    assert hits["total"]["value"] > 0
    src = hits["hits"][0]["_source"]
    assert src["meta"] == {"case": "it"}
    assert src["Event"]["EventData"]["Data"]["param2"] in {"stopped", "running"}

    by_day = es.search(
        index=index,
        size=0,
        aggs={"days": {"date_histogram": {"field": "@timestamp", "calendar_interval": "day"}}},
    )["aggregations"]["days"]["buckets"]
    assert sum(b["doc_count"] for b in by_day) == 1601


def test_load_security_log_into_dynamic_index(es, index, data_dir):
    """No pre-created mapping: dynamic mapping must cope with every record shape."""
    result = EvtxToElk(es, index=index, ecs=False).load(str(data_dir / "security.evtx"))
    assert result.ok, result.errors
    assert result.indexed == 2261
    assert _count(es, index) == 2261
    logons = es.count(index=index, query={"term": {"Event.System.EventID.#text.keyword": "4624"}})[
        "count"
    ]
    assert logons > 0


def test_malformed_file_loads_what_it_can(es, index, data_dir):
    from evtxtoelk.parsers import PYTHON, select_backend

    result = EvtxToElk(es, index=index, ecs=False).load(str(data_dir / "dns_log_malformed.evtx"))
    if select_backend() == PYTHON:
        assert (result.indexed, result.failed, result.skipped) == (1, 0, 4)
    else:
        assert (result.indexed, result.failed, result.skipped) == (5, 0, 0)
    assert _count(es, index) == result.indexed


def test_legacy_api_with_bare_host(es, es_url, index, data_dir):
    from urllib.parse import urlsplit

    parts = urlsplit(es_url)
    bare = f"{parts.hostname}:{parts.port or 9200}"
    result = EvtxToElk.evtx_to_elk(str(data_dir / "issue_38.evtx"), bare, elk_index=index)
    assert result.indexed == 1
    assert _count(es, index) == 1


def test_cli_end_to_end(es, es_url, index, data_dir):
    rc = main(
        [
            str(data_dir / "issue_38.evtx"),
            str(data_dir / "system.evtx"),
            es_url,
            "-i",
            index,
            "--create-index",
            "--legacy",
            "-m",
            '{"source": "cli"}',
        ]
    )
    assert rc == 0
    assert _count(es, index) == 1602
    assert es.count(index=index, query={"term": {"meta.source.keyword": "cli"}})["count"] == 1602


def test_ecs_load_and_query(es, es_url, index, data_dir):
    assert ensure_index(es, index) is True  # ECS mapping by default
    result = EvtxToElk(es, index=index, metadata={"observer.name": "lab"}).load(
        str(data_dir / "security.evtx")
    )
    assert result.ok, result.errors
    assert result.indexed == 2261
    assert _count(es, index) == 2261
    # deterministic ids: a second load is idempotent
    again = EvtxToElk(es, index=index, metadata={"observer.name": "lab"}).load(
        str(data_dir / "security.evtx")
    )
    assert again.ok
    assert _count(es, index) == 2261
    logons = es.count(index=index, query={"term": {"event.code": "4624"}})["count"]
    assert logons > 0
    assert es.count(index=index, query={"term": {"event.action": "logged-in"}})["count"] == logons
    hit = es.search(index=index, query={"term": {"event.code": "4624"}}, size=1)["hits"]["hits"][0][
        "_source"
    ]
    assert es.count(index=index, query={"exists": {"field": "winlog.logon.type"}})["count"] > 0
    assert hit["observer"]["name"] == "lab"
    assert isinstance(hit["winlog"]["process"]["pid"], int)
    mapping = es.indices.get_mapping(index=index)[index]["mappings"]["properties"]
    assert mapping["source"]["properties"]["ip"]["type"] == "ip"
    assert mapping["process"]["properties"]["pid"]["type"] == "long"
    # Elasticsearch reports object fields by their properties, without an explicit type
    assert "properties" in mapping["winlog"]["properties"]["event_data"]
    assert es.count(index=index, query={"range": {"winlog.process.pid": {"gt": 0}}})["count"] > 0
    rc = main([str(data_dir / "issue_38.evtx"), es_url, "-i", index])
    assert rc == 0


def test_every_fixture_module_indexes_into_the_ecs_mapping(es, index, tmp_path):
    """Security, Sysmon and PowerShell documents must all fit the generated mapping."""
    import gzip
    import pathlib

    assert ensure_index(es, index) is True
    root = pathlib.Path(__file__).parent / "data" / "winlogbeat"
    paths = []
    for gz in sorted(root.glob("*/*.evtx.gz")):
        target = tmp_path / gz.name[:-3]
        with gzip.open(gz, "rb") as fh:
            target.write_bytes(fh.read())
        paths.append(str(target))
    result = EvtxToElk(es, index=index).load_many(paths)
    assert result.failed == 0, result.errors[:3]
    assert result.indexed > 200
    for dataset in (
        "system.security",
        "windows.sysmon_operational",
        "windows.powershell_operational",
    ):
        assert _count(es, index, query={"term": {"event.dataset": dataset}}) > 0
    assert _count(es, index, query={"exists": {"field": "process.hash.sha256"}}) > 0
    assert _count(es, index, query={"exists": {"field": "powershell.file.script_block_text"}}) > 0
