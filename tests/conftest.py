import os
import pathlib
import time

import pytest

DATA = pathlib.Path(__file__).parent / "data"

ES_URL = os.environ.get("EVTXTOELK_ES_URL", "http://localhost:9200")
REQUIRE_ES = os.environ.get("EVTXTOELK_REQUIRE_ES", "").lower() in {"1", "true", "yes"}
WAIT_SECONDS = int(os.environ.get("EVTXTOELK_ES_WAIT", "120"))


@pytest.fixture(scope="session")
def data_dir() -> pathlib.Path:
    return DATA


@pytest.fixture(scope="session")
def es_url() -> str:
    """URL of a live Elasticsearch, or skip (fail when EVTXTOELK_REQUIRE_ES is set)."""
    from elasticsearch import Elasticsearch

    # A freshly started cluster (CI service container) can accept TCP connections
    # before it serves HTTP, so keep trying for a while when the tests require it.
    deadline = time.monotonic() + (WAIT_SECONDS if REQUIRE_ES else 5)
    reason = f"Elasticsearch at {ES_URL} did not answer"
    while True:
        client = Elasticsearch(ES_URL, request_timeout=5, max_retries=0)
        try:
            if client.ping():
                return ES_URL
        except Exception as exc:  # noqa: BLE001 - any transport problem means "not yet"
            reason = f"Elasticsearch at {ES_URL} not reachable: {exc}"
        finally:
            client.close()
        if time.monotonic() >= deadline:
            break
        time.sleep(3)
    if REQUIRE_ES:
        pytest.fail(reason)
    pytest.skip(reason)
