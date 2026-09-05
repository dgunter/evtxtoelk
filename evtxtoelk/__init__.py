"""evtxtoelk: load Windows Event Log (.evtx) files into Elasticsearch."""

from evtxtoelk.ecs import ecs_index_body, to_ecs
from evtxtoelk.loader import EvtxToElk, LoadResult, ensure_index, make_client, normalize_url
from evtxtoelk.transform import iter_documents, iter_record_xml, sanitize_key, transform_event

__version__ = "2.1.0"

__all__ = [
    "EvtxToElk",
    "LoadResult",
    "__version__",
    "ecs_index_body",
    "ensure_index",
    "iter_documents",
    "iter_record_xml",
    "make_client",
    "normalize_url",
    "sanitize_key",
    "to_ecs",
    "transform_event",
]
