"""Corpus test against sbousseaden/EVTX-ATTACK-SAMPLES (hundreds of real-world logs).

    git clone --depth 1 https://github.com/sbousseaden/EVTX-ATTACK-SAMPLES \
        .cache/EVTX-ATTACK-SAMPLES
    uv run pytest -m samples

Skipped unless EVTXTOELK_SAMPLES_DIR (default .cache/EVTX-ATTACK-SAMPLES) exists.
The Elasticsearch half additionally needs the docker-compose cluster.
"""

import os
import pathlib
import uuid

import pytest

from evtxtoelk import EvtxToElk, ensure_index, make_client
from evtxtoelk.transform import iter_documents

ROOT = pathlib.Path(__file__).resolve().parents[1]
SAMPLES_DIR = pathlib.Path(
    os.environ.get("EVTXTOELK_SAMPLES_DIR", ROOT / ".cache/EVTX-ATTACK-SAMPLES")
)
SAMPLES = sorted(SAMPLES_DIR.rglob("*.evtx")) if SAMPLES_DIR.is_dir() else []

pytestmark = [
    pytest.mark.samples,
    pytest.mark.skipif(not SAMPLES, reason=f"no .evtx samples under {SAMPLES_DIR}"),
]


def _id(path: pathlib.Path) -> str:
    return str(path.relative_to(SAMPLES_DIR))


@pytest.mark.parametrize("sample", SAMPLES, ids=_id)
def test_every_sample_transforms(sample):
    skipped = []
    docs = list(iter_documents(str(sample), on_error=lambda o, e: skipped.append((o, e))))
    assert docs or skipped, "file produced neither documents nor errors"
    for doc in docs:
        assert doc["@timestamp"]
        event_data = doc["Event"].get("EventData")
        if event_data is not None and "Data" in event_data:
            assert isinstance(event_data["Data"], dict)
            assert not any(k.startswith(".") or k.endswith(".") for k in event_data["Data"])


@pytest.mark.integration
def test_whole_corpus_indexes_without_bulk_failures(es_url):
    es = make_client(es_url)
    index = f"evtxtoelk-samples-{uuid.uuid4().hex[:8]}"
    try:
        ensure_index(es, index)
        result = EvtxToElk(es, index=index, bulk_size=1000).load_many(str(p) for p in SAMPLES)
        es.indices.refresh(index=index)
        assert result.failed == 0, result.errors
        assert es.count(index=index)["count"] == result.indexed
        assert result.indexed > 0
        print(f"\ncorpus: files={len(SAMPLES)} indexed={result.indexed} skipped={result.skipped}")
    finally:
        es.indices.delete(index=index, ignore_unavailable=True)
        es.close()
