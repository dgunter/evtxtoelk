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


def test_clean_xml_strips_declaration_and_escapes_control_chars():
    raw = '<?xml version="1.0" encoding="utf-8"?>\n<Event><Data>a\x03b\tc</Data></Event>'
    assert parsers._clean_xml(raw) == "<Event><Data>a\\x03b\tc</Data></Event>"


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
            _normalise_timestamps(a)
            _normalise_timestamps(b)
            assert a == b


def _normalise_timestamps(doc):
    """The Rust parser truncates 100 ns ticks to microseconds; python-evtx rounded them."""
    doc.get("event", {}).pop("original", None)
    doc["@timestamp"] = doc["@timestamp"][:23]
    doc["event"]["created"] = doc["event"]["created"][:23]
    doc["winlog"]["time_created"] = doc["winlog"]["time_created"][:23]
