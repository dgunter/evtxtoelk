import re
import sys
import types
from datetime import datetime

import pytest

from evtxtoelk import parsers


def test_available_and_select(monkeypatch):
    monkeypatch.delenv(parsers.BACKEND_ENV, raising=False)  # CI forces a backend via the env
    backends = parsers.available_backends()
    assert backends
    assert backends[0] in (parsers.RUST, parsers.PYTHON)
    assert parsers.select_backend() == backends[0]
    assert parsers.select_backend(parsers.AUTO) == backends[0]
    assert parsers.select_backend(backends[0]) == backends[0]


def test_select_rejects_unknown_and_missing(monkeypatch):
    with pytest.raises(ValueError):
        parsers.select_backend("perl")
    monkeypatch.setattr(parsers, "available_backends", lambda: [parsers.RUST])
    with pytest.raises(ImportError):
        parsers.select_backend(parsers.PYTHON)
    monkeypatch.setattr(parsers, "available_backends", lambda: [])
    with pytest.raises(ImportError):
        parsers.select_backend()


def test_environment_variable_selects_backend(monkeypatch):
    monkeypatch.setattr(parsers, "available_backends", lambda: [parsers.RUST, parsers.PYTHON])
    monkeypatch.setenv(parsers.BACKEND_ENV, "python")
    assert parsers.select_backend() == parsers.PYTHON
    monkeypatch.delenv(parsers.BACKEND_ENV)
    assert parsers.select_backend() == parsers.RUST


def test_python_binary_values_render_as_hex():
    class Node:
        def binary(self):
            return b"T\x00r\x00"

    assert parsers._binary_as_hex(Node()) == "54007200"


def test_load_python_patches_binary_rendering(monkeypatch):
    class BinaryTypeNode:
        string = None

    evtx_mod = types.SimpleNamespace(Evtx=object)
    nodes_mod = types.SimpleNamespace(BinaryTypeNode=BinaryTypeNode)
    monkeypatch.setitem(sys.modules, "Evtx.Evtx", evtx_mod)
    monkeypatch.setitem(sys.modules, "Evtx.Nodes", nodes_mod)
    assert parsers._load_python() is evtx_mod
    assert BinaryTypeNode.string is parsers._binary_as_hex


def test_clean_xml_strips_declaration_and_escapes_control_chars():
    raw = '<?xml version="1.0" encoding="utf-8"?>\n<Event><Data>a\x03b\tc</Data></Event>'
    assert parsers._clean_xml(raw) == "<Event><Data>ab\tc</Data></Event>"


def test_detection_is_by_attribute(monkeypatch):
    """`import evtx` may resolve to python-evtx's `Evtx` package on case-insensitive filesystems."""
    import types

    monkeypatch.setattr(parsers.importlib, "import_module", lambda name: types.SimpleNamespace())
    assert parsers._load_rust() is None
    assert parsers._load_python() is None


def test_rust_iterator_skips_a_record_that_fails(monkeypatch):
    class FakeParser:
        def __init__(self, path):
            self.path = path

        def records(self):
            yield {"data": "<Event>1</Event>"}
            raise_after = [RuntimeError("bad record")]
            yield from ()  # pragma: no cover - keeps generator semantics
            raise raise_after[0]

    calls = {"n": 0}

    def records_with_failure(self):
        calls["n"] += 1
        yield {"data": '<?xml version="1.0"?><Event>1</Event>'}
        raise RuntimeError("bad record")

    FakeParser.records = records_with_failure
    monkeypatch.setattr(parsers, "_load_rust", lambda: type("M", (), {"PyEvtxParser": FakeParser}))
    seen = []
    out = list(parsers._iter_rust("x.evtx", lambda offset, exc: seen.append((offset, str(exc)))))
    assert out == ["<Event>1</Event>"]
    assert seen == [(-1, "bad record")]


def test_python_backend_iterates_records(monkeypatch):
    class Record:
        def __init__(self, xml):
            self._xml = xml

        def offset(self):
            return 10

        def xml(self):
            if self._xml is None:
                raise UnicodeDecodeError("ascii", b"\xa6", 0, 1, "ordinal not in range")
            return self._xml

    class Chunk:
        def offset(self):
            return 4096

        def records(self):
            return [Record("<Event>a</Event>"), Record(None), Record("<Event>b</Event>")]

    class FakeEvtx:
        def __init__(self, path):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def chunks(self):
            return [Chunk()]

    monkeypatch.setattr(parsers, "_load_python", lambda: type("M", (), {"Evtx": FakeEvtx}))
    monkeypatch.setattr(parsers, "_load_rust", lambda: None)
    monkeypatch.delenv(parsers.BACKEND_ENV, raising=False)
    assert parsers.available_backends() == [parsers.PYTHON]
    seen = []
    out = list(parsers.iter_record_xml("x.evtx", on_error=lambda o, e: seen.append(o)))
    assert out == ["<Event>a</Event>", "<Event>b</Event>"]
    assert seen == [10]


def test_load_helpers_handle_missing_modules(monkeypatch):
    def boom(name):
        raise ImportError(name)

    monkeypatch.setattr(parsers.importlib, "import_module", boom)
    assert parsers._load_rust() is None
    assert parsers._load_python() is None
    assert parsers.available_backends() == []


_TIME_LIKE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$")


def _comparable(doc):
    """Drop what the two parsers legitimately render differently.

    ``event.original`` is the raw XML. Positional ``paramN`` values are dropped because
    python-evtx renders multi-value substitutions as one ``<string>``-tagged blob, which
    the Rust parser renders as separate elements.
    """
    doc.get("event", {}).pop("original", None)
    event_data = doc.get("winlog", {}).get("event_data", {})
    for key in [k for k in event_data if k.startswith("param")]:
        del event_data[key]
    return doc


def _assert_equivalent(a, b, path=""):
    """Equal, except time strings may differ by a millisecond (truncation vs rounding)."""
    if isinstance(a, dict) and isinstance(b, dict):
        assert a.keys() == b.keys(), path
        for key in a:
            _assert_equivalent(a[key], b[key], f"{path}.{key}")
    elif isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        for i, (x, y) in enumerate(zip(a, b, strict=True)):
            _assert_equivalent(x, y, f"{path}[{i}]")
    elif isinstance(a, str) and isinstance(b, str) and _TIME_LIKE.match(a) and _TIME_LIKE.match(b):
        ta = datetime.fromisoformat(a.replace("Z", "+00:00"))
        tb = datetime.fromisoformat(b.replace("Z", "+00:00"))
        assert abs((ta - tb).total_seconds()) <= 0.001, (path, a, b)
    else:
        assert a == b, path


@pytest.mark.skipif(
    len(parsers.available_backends()) < 2, reason="both parser backends must be installed"
)
def test_backends_produce_identical_documents(data_dir):
    """With both parsers installed (Linux CI), ECS output must not depend on the backend."""
    from evtxtoelk.ecs import to_ecs

    for name in ("security.evtx", "system.evtx", "issue_38.evtx"):
        path = str(data_dir / name)
        rust = [to_ecs(x) for x in parsers.iter_record_xml(path, backend=parsers.RUST)]
        python = [to_ecs(x) for x in parsers.iter_record_xml(path, backend=parsers.PYTHON)]
        assert len(rust) == len(python)
        for a, b in zip(rust, python, strict=True):
            _assert_equivalent(_comparable(a), _comparable(b), name)
