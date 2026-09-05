import json
from unittest import mock

import pytest

from evtxtoelk import cli
from evtxtoelk.loader import LoadResult


def test_dry_run_prints_ndjson(capsys, data_dir):
    rc = cli.main(
        [str(data_dir / "issue_38.evtx"), "localhost", "--dry-run", "--legacy", "-m", '{"c": 1}']
    )
    assert rc == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 1
    doc = json.loads(lines[0])
    assert doc["meta"] == {"c": 1}
    assert doc["Event"]["EventData"]["Data"]["SubjectUserName"] == "foobar"


def test_dry_run_accepts_multiple_files(capsys, data_dir):
    rc = cli.main(
        [str(data_dir / "issue_38.evtx"), str(data_dir / "issue_38.evtx"), "localhost", "--dry-run"]
    )
    assert rc == 0
    assert len(capsys.readouterr().out.strip().splitlines()) == 2


@pytest.mark.parametrize("flag", ["-m", "-meta", "--meta"])
def test_meta_flag_aliases(flag):
    args = cli.build_parser().parse_args(["f.evtx", "localhost", flag, '{"x": "y"}'])
    assert args.meta == {"x": "y"}


@pytest.mark.parametrize("bad", ["not json", "[1, 2]", '"str"'])
def test_meta_must_be_json_object(bad, capsys):
    parser = cli.build_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["f.evtx", "localhost", "-m", bad])
    assert exc.value.code == 2
    assert "meta" in capsys.readouterr().err.lower()


def test_legacy_short_flags_still_parse():
    args = cli.build_parser().parse_args(["f.evtx", "10.0.0.1:9200", "-i", "sec", "-s", "50"])
    assert args.index == "sec"
    assert args.bulk_size == 50
    assert args.evtxfile == ["f.evtx"]
    assert args.destination == "10.0.0.1:9200"


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    assert "evtxtoelk 2." in capsys.readouterr().out


def _run_with_mocks(argv, result, create_index_return=False):
    es = mock.Mock()
    with (
        mock.patch.object(cli, "make_client", return_value=es) as make_client,
        mock.patch.object(cli, "ensure_index", return_value=create_index_return) as ensure,
        mock.patch.object(cli.EvtxToElk, "load_many", return_value=result) as load_many,
    ):
        rc = cli.main(argv)
    return rc, make_client, ensure, load_many, es


def test_main_passes_connection_options_through():
    rc, make_client, ensure, load_many, _ = _run_with_mocks(
        [
            "a.evtx",
            "b.evtx",
            "https://es:9200",
            "-u",
            "elastic",
            "-p",
            "pw",
            "--api-key",
            "k",
            "--ca-certs",
            "ca.pem",
            "-k",
            "--timeout",
            "9",
            "-i",
            "idx",
        ],
        LoadResult(indexed=2),
    )
    assert rc == 0
    make_client.assert_called_once_with(
        "https://es:9200",
        user="elastic",
        password="pw",
        api_key="k",
        ca_certs="ca.pem",
        verify_certs=False,
        timeout=9.0,
    )
    ensure.assert_not_called()
    load_many.assert_called_once_with(["a.evtx", "b.evtx"])


def test_main_creates_index_when_asked(caplog):
    rc, _, ensure, _, es = _run_with_mocks(
        ["a.evtx", "localhost", "--create-index"], LoadResult(indexed=1), create_index_return=True
    )
    assert rc == 0
    ensure.assert_called_once_with(es, "hostlogs", ecs=True)
    assert "created index hostlogs" in caplog.text


def test_main_returns_1_on_bulk_failures(caplog):
    rc, *_ = _run_with_mocks(
        ["a.evtx", "localhost"], LoadResult(indexed=1, failed=2, errors=["boom"])
    )
    assert rc == 1
    assert "boom" in caplog.text


def test_main_prompts_for_password_when_user_given():
    es = mock.Mock()
    with (
        mock.patch.object(cli, "make_client", return_value=es) as make_client,
        mock.patch.object(cli.EvtxToElk, "load_many", return_value=LoadResult()),
        mock.patch("getpass.getpass", return_value="typed"),
    ):
        assert cli.main(["a.evtx", "localhost", "-u", "elastic"]) == 0
    assert make_client.call_args.kwargs["password"] == "typed"


def test_main_reports_transport_errors(caplog):
    from elastic_transport import ConnectionError as ESConnectionError

    es = mock.Mock()
    with (
        mock.patch.object(cli, "make_client", return_value=es),
        mock.patch.object(cli.EvtxToElk, "load_many", side_effect=ESConnectionError("refused")),
    ):
        assert cli.main(["a.evtx", "localhost"]) == 1
    assert "Elasticsearch error" in caplog.text


def test_main_reports_missing_file(caplog):
    es = mock.Mock()
    with (
        mock.patch.object(cli, "make_client", return_value=es),
        mock.patch.object(cli.EvtxToElk, "load_many", side_effect=FileNotFoundError("a.evtx")),
    ):
        assert cli.main(["a.evtx", "localhost"]) == 1
    assert "a.evtx" in caplog.text


def test_output_file_writes_ndjson(tmp_path, data_dir, caplog):
    out = tmp_path / "events.ndjson"
    rc = cli.main([str(data_dir / "issue_38.evtx"), "localhost", "-o", str(out)])
    assert rc == 0
    lines = out.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["host"]["name"] == "foobar-PC"
    assert f"1 events exported to {out}" in caplog.text


@pytest.mark.parametrize("name", ["events.json", "Events.JSON", "e.jsonl", "e.ndjson"])
def test_json_destination_selects_file_export(tmp_path, data_dir, name):
    """PR #4 behaviour: a destination ending in .json writes a file instead of indexing."""
    out = tmp_path / name
    with mock.patch.object(cli, "make_client") as make_client:
        rc = cli.main([str(data_dir / "issue_38.evtx"), str(out)])
    assert rc == 0
    make_client.assert_not_called()
    assert len(out.read_text(encoding="utf-8").splitlines()) == 1


def test_dash_destination_is_stdout(capsys, data_dir):
    rc = cli.main([str(data_dir / "issue_38.evtx"), "-"])
    assert rc == 0
    assert len(capsys.readouterr().out.strip().splitlines()) == 1


def test_output_dash_is_stdout(capsys, data_dir):
    rc = cli.main([str(data_dir / "issue_38.evtx"), "localhost", "--output", "-"])
    assert rc == 0
    assert len(capsys.readouterr().out.strip().splitlines()) == 1


def test_output_file_overwrites_and_covers_multiple_inputs(tmp_path, data_dir):
    out = tmp_path / "all.json"
    out.write_text("stale\n")
    count = cli.write_json_lines(
        [str(data_dir / "issue_38.evtx"), str(data_dir / "system.evtx")],
        str(out),
        {"m": 1},
        ecs=False,
    )
    assert count == 1602
    lines = out.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1602
    assert all(json.loads(line)["meta"] == {"m": 1} for line in lines)


def test_output_to_unwritable_path_returns_1(tmp_path, data_dir, caplog):
    rc = cli.main([str(data_dir / "issue_38.evtx"), str(tmp_path / "missing" / "x.json")])
    assert rc == 1
    assert "missing" in caplog.text


def test_resolve_output_path_expands_and_validates(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    target = cli.resolve_output_path("~/out.json")
    assert target == tmp_path / "out.json"
    with pytest.raises(IsADirectoryError):
        cli.resolve_output_path(str(tmp_path))
    with pytest.raises(FileNotFoundError):
        cli.resolve_output_path(str(tmp_path / "nope" / "out.json"))


def test_verbose_logs_traceback_on_failure(caplog):
    from elastic_transport import ConnectionError as ESConnectionError

    es = mock.Mock()
    with (
        mock.patch.object(cli, "make_client", return_value=es),
        mock.patch.object(cli.EvtxToElk, "load_many", side_effect=ESConnectionError("refused")),
    ):
        assert cli.main(["a.evtx", "localhost", "-v"]) == 1
    assert "Elasticsearch error: " in caplog.text
    assert "Traceback" in caplog.text
