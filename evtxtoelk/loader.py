"""Bulk-load transformed evtx documents into Elasticsearch."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from elasticsearch import Elasticsearch, helpers

from evtxtoelk.transform import iter_documents

log = logging.getLogger(__name__)

DEFAULT_INDEX = "hostlogs"
DEFAULT_BULK_SIZE = 500
DEFAULT_PORT = 9200

#: Mapping applied by ``ensure_index``. Dynamic detection is switched off so a
#: value that happens to look like a date or number in one record does not lock
#: the field to that type and reject every later record.
INDEX_BODY: dict[str, Any] = {
    "mappings": {
        "date_detection": False,
        "numeric_detection": False,
        "properties": {
            "@timestamp": {"type": "date"},
            "Event": {
                "properties": {
                    "System": {
                        "properties": {
                            "TimeCreated": {"properties": {"@SystemTime": {"type": "date"}}},
                        }
                    }
                }
            },
        },
    }
}


def normalize_url(host: str) -> str:
    """Accept the host forms earlier releases took and return a full URL.

    ``localhost`` -> ``http://localhost:9200``, ``10.0.0.5:9201`` ->
    ``http://10.0.0.5:9201``; anything with a scheme is returned unchanged.
    """
    host = host.strip()
    if "://" not in host:
        # Plain HTTP is what a bare host meant in 1.x and what a dev cluster with
        # security disabled speaks; pass an https:// URL for anything else.
        host = f"http://{host}"  # NOSONAR python:S5332 - explicit scheme wins, see docstring
    parts = urlsplit(host)
    if parts.port is None and parts.scheme == "http":
        netloc = f"{parts.hostname}:{DEFAULT_PORT}"
        if parts.username:
            auth = parts.username
            if parts.password is not None:
                auth = f"{auth}:{parts.password}"
            netloc = f"{auth}@{netloc}"
        host = parts._replace(netloc=netloc).geturl()
    return host


def make_client(
    url: str,
    *,
    user: str | None = None,
    password: str | None = None,
    api_key: str | None = None,
    ca_certs: str | None = None,
    verify_certs: bool = True,
    timeout: float = 60,
) -> Elasticsearch:
    """Build an ``Elasticsearch`` client from CLI-style options."""
    kwargs: dict[str, Any] = {"request_timeout": timeout}
    if user is not None:
        kwargs["basic_auth"] = (user, password or "")
    if api_key is not None:
        kwargs["api_key"] = api_key
    if ca_certs is not None:
        kwargs["ca_certs"] = ca_certs
    if not verify_certs:
        kwargs["verify_certs"] = False
        kwargs["ssl_show_warn"] = False
    return Elasticsearch(normalize_url(url), **kwargs)


def ensure_index(es: Elasticsearch, index: str) -> bool:
    """Create ``index`` with the evtxtoelk mapping if it does not exist. Returns True if created."""
    if es.indices.exists(index=index):
        return False
    es.indices.create(index=index, **INDEX_BODY)
    return True


@dataclass
class LoadResult:
    """Outcome of loading one file."""

    indexed: int = 0
    failed: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.failed == 0


class EvtxToElk:
    """Stream the records of .evtx files into an Elasticsearch index."""

    def __init__(
        self,
        es: Elasticsearch,
        index: str = DEFAULT_INDEX,
        bulk_size: int = DEFAULT_BULK_SIZE,
        metadata: dict[str, Any] | None = None,
        max_error_samples: int = 10,
    ) -> None:
        self.es = es
        self.index = index
        self.bulk_size = max(1, int(bulk_size))
        self.metadata = dict(metadata) if metadata else None
        self.max_error_samples = max_error_samples

    def actions(self, path: str, result: LoadResult | None = None) -> Iterator[dict[str, Any]]:
        """Yield bulk actions for ``path``; unreadable records are counted on ``result``."""
        result = result if result is not None else LoadResult()

        def on_error(_offset: int, _exc: Exception) -> None:
            result.skipped += 1

        for doc in iter_documents(path, self.metadata, on_error=on_error):
            yield {"_index": self.index, "_source": doc}

    def load(self, path: str) -> LoadResult:
        """Index every readable record of ``path`` and return counts."""
        result = LoadResult()
        stream = helpers.streaming_bulk(
            self.es,
            self.actions(path, result),
            chunk_size=self.bulk_size,
            raise_on_error=False,
            max_retries=3,
        )
        self._consume(stream, result)
        log.info(
            "%s: indexed=%d failed=%d skipped=%d",
            path,
            result.indexed,
            result.failed,
            result.skipped,
        )
        return result

    def load_many(self, paths: Iterable[str]) -> LoadResult:
        """Load several files and return the combined counts."""
        total = LoadResult()
        for path in paths:
            one = self.load(path)
            total.indexed += one.indexed
            total.failed += one.failed
            total.skipped += one.skipped
            total.errors.extend(one.errors[: max(0, self.max_error_samples - len(total.errors))])
        return total

    def _consume(self, stream: Iterable[tuple[bool, Any]], result: LoadResult) -> None:
        for ok, item in stream:
            if ok:
                result.indexed += 1
                continue
            result.failed += 1
            if len(result.errors) < self.max_error_samples:
                result.errors.append(str(item))

    # -- backwards compatibility -------------------------------------------------

    @staticmethod
    def evtx_to_elk(
        filename: str,
        elk_ip: str,
        elk_index: str = DEFAULT_INDEX,
        bulk_queue_len_threshold: int = DEFAULT_BULK_SIZE,
        metadata: dict[str, Any] | None = None,
    ) -> LoadResult:
        """The 1.x entry point: ``EvtxToElk.evtx_to_elk("file.evtx", "localhost:9200")``."""
        es = make_client(elk_ip)
        loader = EvtxToElk(
            es, index=elk_index, bulk_size=bulk_queue_len_threshold, metadata=metadata
        )
        return loader.load(filename)
