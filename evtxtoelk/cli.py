"""Command line interface: ``evtxtoelk FILE... URL`` or ``evtxtoelk FILE... out.json``."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

from elasticsearch import ApiError, TransportError

from evtxtoelk import __version__
from evtxtoelk.loader import DEFAULT_BULK_SIZE, DEFAULT_INDEX, EvtxToElk, ensure_index, make_client
from evtxtoelk.transform import iter_documents

log = logging.getLogger(__name__)


def _json_object(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError(f"not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise argparse.ArgumentTypeError("metadata must be a JSON object")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="evtxtoelk",
        description="Load Windows Event Log (.evtx) files into Elasticsearch.",
    )
    parser.add_argument("evtxfile", nargs="+", help="one or more .evtx files to load")
    parser.add_argument(
        "destination",
        help=(
            "Elasticsearch URL, e.g. http://localhost:9200 (a bare host:port is accepted), "
            "a path ending in .json / .jsonl / .ndjson to write JSON lines instead, "
            "or '-' for JSON lines on stdout"
        ),
    )
    parser.add_argument(
        "-i", "--index", default=DEFAULT_INDEX, help=f"target index (default: {DEFAULT_INDEX})"
    )
    parser.add_argument(
        "-s",
        "--bulk-size",
        type=int,
        default=DEFAULT_BULK_SIZE,
        help=f"documents per bulk request (default: {DEFAULT_BULK_SIZE})",
    )
    parser.add_argument(
        "-m",
        "-meta",
        "--meta",
        dest="meta",
        type=_json_object,
        default=None,
        metavar="JSON",
        help='JSON object stored under "meta" on every document, e.g. \'{"case": "1234"}\'',
    )
    auth = parser.add_argument_group("authentication and TLS")
    auth.add_argument("-u", "--user", help="basic-auth user name")
    auth.add_argument("-p", "--password", help="basic-auth password (prompted if omitted)")
    auth.add_argument("--api-key", help="Elasticsearch API key (base64 id:key form)")
    auth.add_argument("--ca-certs", metavar="PEM", help="CA bundle used to verify the server")
    auth.add_argument(
        "-k",
        "--insecure",
        action="store_true",
        help="do not verify the server certificate (self-signed clusters)",
    )
    parser.add_argument(
        "--timeout", type=float, default=60, help="request timeout in seconds (default: 60)"
    )
    parser.add_argument(
        "--create-index",
        action="store_true",
        help="create the index with the recommended mapping if it does not exist",
    )
    layout = parser.add_argument_group("document layout")
    layout.add_argument(
        "--legacy",
        action="store_true",
        help="emit the 2.0 layout (Event.System.*, Event.EventData.Data.*) instead of ECS",
    )
    layout.add_argument(
        "--ecs-original",
        action="store_true",
        help="include the record XML as event.original (ECS layout only)",
    )
    layout.add_argument(
        "--no-dedupe",
        action="store_true",
        help="let Elasticsearch assign ids instead of one derived from host, channel and record id",
    )
    out = parser.add_argument_group("file output (no Elasticsearch needed)")
    out.add_argument(
        "-o",
        "--output",
        metavar="FILE",
        help="write documents as JSON lines to FILE ('-' for stdout) instead of indexing",
    )
    out.add_argument(
        "--dry-run",
        action="store_true",
        help="shorthand for --output - (JSON lines on stdout)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


JSON_SUFFIXES = (".json", ".jsonl", ".ndjson")


def _looks_like_json_path(destination: str) -> bool:
    return destination == "-" or destination.lower().endswith(JSON_SUFFIXES)


def _documents(
    paths: Sequence[str],
    metadata: dict[str, Any] | None,
    *,
    ecs: bool,
    original: bool,
) -> Iterator[dict[str, Any]]:
    if not ecs:
        for path in paths:
            yield from iter_documents(path, metadata)
        return
    from evtxtoelk.ecs import to_ecs
    from evtxtoelk.transform import iter_record_xml

    for path in paths:
        for xml in iter_record_xml(path):
            try:
                yield to_ecs(xml, original=original, meta=metadata)
            except Exception as exc:  # noqa: BLE001 - one bad record must not stop the export
                log.warning("skipping record that could not be mapped to ECS: %s", exc)


def write_json_lines(
    paths: Sequence[str],
    output: str,
    metadata: dict[str, Any] | None = None,
    *,
    ecs: bool = True,
    original: bool = False,
) -> int:
    """Write one JSON document per line to ``output`` ('-' is stdout). Returns the count.

    This is the file export contributed by @okynos in PR #4 so the same documents
    can be fed to any collector that reads JSON (Wazuh, Filebeat, ...).
    """
    count = 0
    docs = _documents(paths, metadata, ecs=ecs, original=original)
    if output == "-":
        for doc in docs:
            sys.stdout.write(json.dumps(doc) + "\n")
            count += 1
        return count
    with resolve_output_path(output).open("w", encoding="utf-8") as handle:
        for doc in docs:
            handle.write(json.dumps(doc) + "\n")
            count += 1
    return count


def resolve_output_path(output: str) -> Path:
    """Turn a user-supplied export path into an absolute file path we are willing to write.

    The parent directory must already exist and the target must not be a
    directory, so a mistyped path fails with a clear message instead of
    creating files somewhere unexpected.
    """
    target = Path(output).expanduser().resolve()
    if not target.parent.is_dir():
        raise FileNotFoundError(f"output directory does not exist: {target.parent}")
    if target.is_dir():
        raise IsADirectoryError(f"output path is a directory: {target}")
    return target


def _fail(message: str, exc: BaseException) -> int:
    """Log a one-line error for the user (traceback only at debug level) and return exit 1."""
    log.error("%s: %s", message, exc)
    log.debug("traceback:", exc_info=exc)
    return 1


def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format="%(levelname)s %(message)s", stream=sys.stderr)
    logging.getLogger("evtxtoelk").setLevel(level)
    logging.getLogger("elastic_transport").setLevel(logging.WARNING)
    logging.getLogger("Evtx").setLevel(logging.ERROR)


def _export_target(args: argparse.Namespace) -> str | None:
    """Where JSON lines should go, or None when the run indexes into Elasticsearch."""
    if args.dry_run:
        return "-"
    if args.output is not None:
        return args.output
    if _looks_like_json_path(args.destination):
        return args.destination
    return None


def _run_export(args: argparse.Namespace, output: str) -> int:
    try:
        count = write_json_lines(
            args.evtxfile, output, args.meta, ecs=not args.legacy, original=args.ecs_original
        )
    except OSError as exc:
        return _fail("cannot write output", exc)
    if output != "-":
        log.info("%d events exported to %s", count, output)
    return 0


def _run_index(args: argparse.Namespace) -> int:
    password = args.password
    if args.user and password is None:
        import getpass

        password = getpass.getpass(f"Password for {args.user}: ")

    es = make_client(
        args.destination,
        user=args.user,
        password=password,
        api_key=args.api_key,
        ca_certs=args.ca_certs,
        verify_certs=not args.insecure,
        timeout=args.timeout,
    )
    loader = EvtxToElk(
        es,
        index=args.index,
        bulk_size=args.bulk_size,
        metadata=args.meta,
        ecs=not args.legacy,
        original=args.ecs_original,
        dedupe=not args.no_dedupe,
    )
    try:
        if args.create_index and ensure_index(es, args.index, ecs=not args.legacy):
            log.info("created index %s", args.index)
        result = loader.load_many(args.evtxfile)
    except (ApiError, TransportError) as exc:
        return _fail("Elasticsearch error", exc)
    except OSError as exc:
        return _fail("cannot read input", exc)

    log.info("done: indexed=%d failed=%d skipped=%d", result.indexed, result.failed, result.skipped)
    for err in result.errors:
        log.error("bulk failure: %s", err)
    return 0 if result.ok else 1


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _configure_logging(args.verbose)
    output = _export_target(args)
    if output is not None:
        return _run_export(args, output)
    return _run_index(args)


if __name__ == "__main__":
    sys.exit(main())
