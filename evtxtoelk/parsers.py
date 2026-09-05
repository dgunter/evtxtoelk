"""The evtx parser backend: the Rust-backed ``evtx`` wheels when installed, python-evtx otherwise.

Exactly one of the two is installed per platform (see ``pyproject.toml``); both
yield the same per-record XML, which everything downstream consumes. Detection is
by attribute rather than module name because the two packages differ only by
case (``evtx`` / ``Evtx``) and resolve to each other on case-insensitive
filesystems.
"""

from __future__ import annotations

import importlib
import logging
import os
import re
from collections.abc import Callable, Iterator
from typing import Any

log = logging.getLogger(__name__)

ErrorHook = Callable[[int, Exception], None]

RUST = "rust"
PYTHON = "python"
AUTO = "auto"
#: Environment variable that forces a backend (``rust`` or ``python``).
BACKEND_ENV = "EVTXTOELK_PARSER"

_XML_DECLARATION = re.compile(r"^\s*<\?xml[^>]*\?>\s*")
#: Characters XML 1.0 forbids even as references; the Rust parser emits them raw.
_INVALID_XML_CHARS = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")


def _clean_xml(text: str) -> str:
    """Drop the XML declaration and the control characters XML forbids (as python-evtx does)."""
    text = _XML_DECLARATION.sub("", text, count=1)
    return _INVALID_XML_CHARS.sub("", text)


def _load_rust() -> Any | None:
    try:
        module = importlib.import_module("evtx")
    except ImportError:
        return None
    return module if hasattr(module, "PyEvtxParser") else None


def _binary_as_hex(node: Any) -> str:
    """Render binary values as uppercase hex, as Windows and the Rust parser do (not base64)."""
    return node.binary().hex().upper()


def _load_python() -> Any | None:
    try:
        module = importlib.import_module("Evtx.Evtx")
        nodes = importlib.import_module("Evtx.Nodes")
    except ImportError:
        return None
    if not hasattr(module, "Evtx"):
        return None
    nodes.BinaryTypeNode.string = _binary_as_hex
    return module


def available_backends() -> list[str]:
    """Backends importable in this environment, fastest first."""
    found = []
    if _load_rust() is not None:
        found.append(RUST)
    if _load_python() is not None:
        found.append(PYTHON)
    return found


def select_backend(preferred: str | None = None) -> str:
    """Pick a backend: ``preferred`` (or ``EVTXTOELK_PARSER``) if importable, else the fastest."""
    backends = available_backends()
    if not backends:
        raise ImportError(
            "no evtx parser is installed: install 'evtx' (Rust wheels) or 'python-evtx'"
        )
    if preferred is None:
        preferred = os.environ.get(BACKEND_ENV) or AUTO
    if preferred == AUTO:
        return backends[0]
    if preferred not in (RUST, PYTHON):
        raise ValueError(f"unknown parser backend {preferred!r}")
    if preferred not in backends:
        raise ImportError(f"parser backend {preferred!r} is not installed (have: {backends})")
    return preferred


def _iter_rust(path: str, on_error: ErrorHook | None) -> Iterator[str]:
    module = _load_rust()
    records = module.PyEvtxParser(os.fspath(path)).records()
    while True:
        try:
            record = next(records)
        except StopIteration:
            return
        except Exception as exc:  # noqa: BLE001 - the parser reports one bad record at a time
            log.warning("skipping unreadable record: %s", exc)
            if on_error:
                on_error(-1, exc)
            continue
        yield _clean_xml(record["data"])


def _iter_python(path: str, on_error: ErrorHook | None) -> Iterator[str]:
    module = _load_python()
    with module.Evtx(os.fspath(path)) as evtx:
        for chunk in evtx.chunks():
            try:
                records = list(chunk.records())
            except Exception as exc:  # noqa: BLE001 - python-evtx raises a wide range here
                log.warning("skipping unreadable chunk at offset %s: %s", chunk.offset(), exc)
                if on_error:
                    on_error(chunk.offset(), exc)
                continue
            for record in records:
                try:
                    yield record.xml()
                except Exception as exc:  # noqa: BLE001
                    log.warning("skipping unreadable record at offset %s: %s", record.offset(), exc)
                    if on_error:
                        on_error(record.offset(), exc)


def iter_record_xml(
    path: str | os.PathLike[str],
    on_error: ErrorHook | None = None,
    backend: str | None = None,
) -> Iterator[str]:
    """Yield the XML of every readable record in an .evtx file.

    Corrupt chunks and records are reported through ``on_error`` (offset or -1,
    exception) and skipped instead of aborting the whole file.
    """
    chosen = select_backend(backend)
    log.debug("parsing %s with the %s backend", path, chosen)
    if chosen == RUST:
        yield from _iter_rust(os.fspath(path), on_error)
    else:
        yield from _iter_python(os.fspath(path), on_error)


__all__ = [
    "AUTO",
    "BACKEND_ENV",
    "PYTHON",
    "RUST",
    "available_backends",
    "iter_record_xml",
    "select_backend",
]
