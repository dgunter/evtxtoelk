"""Corpus tests against sbousseaden/EVTX-ATTACK-SAMPLES (hundreds of real-world logs).

    git clone --depth 1 https://github.com/sbousseaden/EVTX-ATTACK-SAMPLES \\
        .cache/EVTX-ATTACK-SAMPLES
    uv run pytest -m samples

Skipped unless EVTXTOELK_SAMPLES_DIR (default .cache/EVTX-ATTACK-SAMPLES) exists.
The Elasticsearch half additionally needs the docker-compose cluster.
"""

import json
import os
import pathlib
import uuid

import pytest

from evtxtoelk import EvtxToElk, ensure_index, make_client
from evtxtoelk._ecs_tables import ARRAY_FIELDS, FIELD_TYPES
from evtxtoelk.ecs import to_ecs
from evtxtoelk.transform import iter_documents, iter_record_xml

ROOT = pathlib.Path(__file__).resolve().parents[1]
SAMPLES_DIR = pathlib.Path(
    os.environ.get("EVTXTOELK_SAMPLES_DIR", ROOT / ".cache/EVTX-ATTACK-SAMPLES")
)
SAMPLES = sorted(SAMPLES_DIR.rglob("*.evtx")) if SAMPLES_DIR.is_dir() else []

pytestmark = [
    pytest.mark.samples,
    pytest.mark.skipif(not SAMPLES, reason=f"no .evtx samples under {SAMPLES_DIR}"),
]

_PY_TYPES = {
    "long": int, "integer": int, "double": float, "float": float, "boolean": bool,
    "ip": str, "date": str, "keyword": str, "text": str, "wildcard": str, "match_only_text": str,
}  # fmt: skip


def _id(path: pathlib.Path) -> str:
    return str(path.relative_to(SAMPLES_DIR))


def _flat(doc, prefix=""):
    out = {}
    for key, value in doc.items():
        name = f"{prefix}{key}"
        if isinstance(value, dict):
            out.update(_flat(value, name + "."))
        else:
            out[name] = value
    return out


@pytest.mark.parametrize("sample", SAMPLES, ids=_id)
def test_every_sample_transforms_legacy(sample):
    skipped = []
    docs = list(iter_documents(str(sample), on_error=lambda o, e: skipped.append((o, e))))
    assert docs or skipped, "file produced neither documents nor errors"
    for doc in docs:
        assert doc["@timestamp"]


@pytest.mark.parametrize("sample", SAMPLES, ids=_id)
def test_every_sample_maps_to_typed_ecs(sample):
    """Every record maps, every emitted field carries its declared type, and it serialises."""
    count = 0
    for xml in iter_record_xml(str(sample)):
        doc = to_ecs(xml)
        flat = _flat(doc)
        assert flat["@timestamp"] and flat["event.code"] == flat["winlog.event_id"]
        assert flat["ecs.version"]
        for key, value in flat.items():
            assert value is not None, key
            if key.startswith(("winlog.event_data.", "winlog.user_data.")):
                continue
            expected = _PY_TYPES.get(FIELD_TYPES.get(key, ""))
            if expected is not None:
                values = value if isinstance(value, list) else [value]
                for item in values:
                    assert isinstance(item, expected), (key, item)
                    assert not (expected is int and isinstance(item, bool)), key
            if key in ARRAY_FIELDS:
                assert isinstance(value, list), key
        json.dumps(doc)
        count += 1
    assert count > 0


@pytest.mark.integration
def test_whole_corpus_indexes_as_ecs_without_bulk_failures(es_url):
    es = make_client(es_url)
    index = f"evtxtoelk-samples-{uuid.uuid4().hex[:8]}"
    try:
        ensure_index(es, index)
        result = EvtxToElk(es, index=index, bulk_size=1000).load_many(str(p) for p in SAMPLES)
        es.indices.refresh(index=index)
        assert result.failed == 0, result.errors
        stored = es.count(index=index)["count"]
        # deterministic ids collapse records that appear in more than one sample file
        assert 0 < stored <= result.indexed
        print(
            f"\ncorpus: files={len(SAMPLES)} indexed={result.indexed} "
            f"distinct={stored} skipped={result.skipped}"
        )
    finally:
        es.indices.delete(index=index, ignore_unavailable=True)
        es.close()
