"""Command line interface: ``evtxtoelk FILE... URL`` or ``evtxtoelk FILE... out.json``."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Sequence
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


def write_json_lines(
    paths: Sequence[str], output: str, metadata: dict[str, Any] | None = None
) -> int:
    """Write one JSON document per line to ``output`` ('-' is stdout). Returns the count.

    This is the file export contributed by @okynos in PR #4 so the same documents
    can be fed to any collector that reads JSON (Wazuh, Filebeat, ...).
    """
    count = 0
    if output == "-":
        for path in paths:
            for doc in iter_documents(path, metadata):
                sys.stdout.write(json.dumps(doc) + "\n")
                count += 1
        return count
    with open(output, "w", encoding="utf-8") as handle:
        for path in paths:
            for doc in iter_documents(path, metadata):
                handle.write(json.dumps(doc) + "\n")
                count += 1
    return count


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=level, format="%(levelname)s %(message)s", stream=sys.stderr)
    logging.getLogger("evtxtoelk").setLevel(level)
    logging.getLogger("elastic_transport").setLevel(logging.WARNING)
    logging.getLogger("Evtx").setLevel(logging.ERROR)

    output = args.output
    if args.dry_run:
        output = "-"
    elif output is None and _looks_like_json_path(args.destination):
        output = args.destination
    if output is not None:
        try:
            count = write_json_lines(args.evtxfile, output, args.meta)
        except OSError as exc:
            log.error("%s", exc)
            return 1
        if output != "-":
            log.info("%d events exported to %s", count, output)
        return 0

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
    loader = EvtxToElk(es, index=args.index, bulk_size=args.bulk_size, metadata=args.meta)
    try:
        if args.create_index and ensure_index(es, args.index):
            log.info("created index %s", args.index)
        result = loader.load_many(args.evtxfile)
    except (ApiError, TransportError) as exc:
        log.error("Elasticsearch error: %s", exc)
        return 1
    except OSError as exc:
        log.error("%s", exc)
        return 1

    log.info("done: indexed=%d failed=%d skipped=%d", result.indexed, result.failed, result.skipped)
    for err in result.errors:
        log.error("bulk failure: %s", err)
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
