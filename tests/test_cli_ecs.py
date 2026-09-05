import json
from unittest import mock

from evtxtoelk import cli
from evtxtoelk.loader import LoadResult


def test_export_defaults_to_ecs(capsys, data_dir):
    assert cli.main([str(data_dir / "issue_38.evtx"), "-", "-m", '{"observer.name": "lab"}']) == 0
    doc = json.loads(capsys.readouterr().out.splitlines()[0])
    assert doc["event"]["code"] == "4672"
    assert doc["event"]["module"] == "security"
    assert doc["winlog"]["event_data"]["SubjectUserName"] == "foobar"
    assert doc["observer"]["name"] == "lab"
    assert "Event" not in doc


def test_legacy_flag_keeps_20_layout(capsys, data_dir):
    assert cli.main([str(data_dir / "issue_38.evtx"), "-", "--legacy"]) == 0
    doc = json.loads(capsys.readouterr().out.splitlines()[0])
    assert doc["Event"]["System"]["EventID"]["#text"] == "4672"
    assert "winlog" not in doc


def test_ecs_original_includes_xml(capsys, data_dir):
    assert cli.main([str(data_dir / "issue_38.evtx"), "-", "--ecs-original"]) == 0
    doc = json.loads(capsys.readouterr().out.splitlines()[0])
    assert doc["event"]["original"].lstrip().startswith("<Event")


def test_loader_flags_reach_the_loader(data_dir):
    es = mock.Mock()
    with (
        mock.patch.object(cli, "make_client", return_value=es),
        mock.patch.object(cli, "ensure_index", return_value=True) as ensure,
        mock.patch.object(cli.EvtxToElk, "__init__", return_value=None) as init,
        mock.patch.object(cli.EvtxToElk, "load_many", return_value=LoadResult(indexed=1)),
    ):
        assert cli.main([str(data_dir / "issue_38.evtx"), "localhost", "--create-index"]) == 0
        assert init.call_args.kwargs["ecs"] is True
        assert init.call_args.kwargs["dedupe"] is True
        ensure.assert_called_once_with(es, "hostlogs", ecs=True)
        init.reset_mock()
        ensure.reset_mock()
        rc = cli.main(
            [
                str(data_dir / "issue_38.evtx"),
                "localhost",
                "--create-index",
                "--legacy",
                "--no-dedupe",
                "--ecs-original",
            ]
        )
        assert rc == 0
        assert init.call_args.kwargs["ecs"] is False
        assert init.call_args.kwargs["dedupe"] is False
        assert init.call_args.kwargs["original"] is True
        ensure.assert_called_once_with(es, "hostlogs", ecs=False)
