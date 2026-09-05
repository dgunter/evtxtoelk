import pytest

from evtxtoelk import parsers


def test_available_and_select():
    backends = parsers.available_backends()
    assert backends and backends[0] in (parsers.RUST, parsers.PYTHON)
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
