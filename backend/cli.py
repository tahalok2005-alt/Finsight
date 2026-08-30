"""FinSight command line interface.

    python cli.py init-db
    python cli.py ingest <file.pdf> [--force-ocr] [--allow-duplicate]
    python cli.py status <document_id>
    python cli.py show <document_id> [--limit 30]
    python cli.py export <document_id> --format json|csv|xlsx [-o out]
    python cli.py evaluate <document_id> [--truth path.json]
    python cli.py baseline <file.pdf>
    python cli.py engines
    python cli.py list
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.config.settings import settings  # noqa: E402
from app.core.ids import new_uuid, sha256_file  # noqa: E402
from app.core.logging import configure_logging, get_logger  # noqa: E402
from app.database import repository as repo  # noqa: E402
from app.database.session import init_db, session_scope  # noqa: E402
from app.ingestion import storage  # noqa: E402
from app.ingestion.validator import probe_pdf  # noqa: E402

log = get_logger("cli")


def cmd_init_db(args) -> int:
    init_db(drop=args.drop)
    print(f"Database ready at {settings.database_url}")
    return 0


def cmd_engines(args) -> int:
    from app.ocr.registry import probe_engines
    print(json.dumps(probe_engines(), indent=2))
    return 0


def cmd_ingest(args) -> int:
    init_db()
    src = Path(args.path).resolve()
    if not src.is_file():
        print(f"error: no such file: {src}", file=sys.stderr)
        return 2

    document_id = new_uuid()[:24]
    stored, size = storage.store_local_file(document_id, src)
    content_hash = sha256_file(stored)

    with session_scope() as session:
        existing = repo.find_by_hash(session, content_hash)
        if existing is not None and not args.allow_duplicate:
            storage.delete_document_files(document_id)
            print(f"Already ingested as {existing.id} (status {existing.status}). "
                  f"Use --allow-duplicate to force.")
            return 1
        probe = probe_pdf(stored)
        doc = repo.create_document(
            session, document_id=document_id, filename=stored.name,
            original_filename=src.name, stored_path=str(stored),
            content_hash=content_hash, size_bytes=size)
        doc.page_count = probe.page_count
        doc.pdf_version = probe.pdf_version
        doc.is_scanned = probe.is_scanned
        doc.text_layer_chars = probe.text_layer_chars
        doc.pdf_metadata = probe.metadata

    print(f"Ingested {src.name} as {document_id} ({probe.page_count} pages, "
          f"{'scanned' if probe.is_scanned else 'digital'})")
    print("Processing...")

    from app.services.processor import process_document
    result = process_document(document_id, trigger="cli",
                              force_ocr=args.force_ocr)

    print(f"\nStatus:      {result.status}")
    print(f"Duration:    {result.duration_ms / 1000:.1f}s")
    print(f"Pages:       {result.pages_done} ok / {result.pages_failed} failed")
    print(f"Tables:      {len(result.tables)}")
    print(f"Charts:      {len(result.charts)}")
    print(f"Facts:       {len(result.facts)}")
    print(f"Commentary:  {len(result.commentary)}")
    print(f"Guidance:    {len(result.guidance)}")
    print(f"Validations: {len(result.validations)}")
    if result.error:
        print(f"Error:       {result.error}")
    print(f"\nDocument id: {document_id}")
    return 0 if str(result.status) != "FAILED" else 1


def cmd_status(args) -> int:
    with session_scope() as session:
        doc = repo.get_document(session, args.document_id)
        if doc is None:
            print("not found", file=sys.stderr)
            return 2
        counts = repo.document_counts(session, args.document_id)
        print(json.dumps({
            "id": doc.id, "filename": doc.original_filename,
            "status": doc.status, "pages": doc.page_count,
            "company": doc.company_name, "period": doc.report_period,
            "currency": doc.document_currency, "unit": doc.document_unit,
            "processing_ms": doc.processing_ms, "error": doc.error, **counts,
        }, indent=2, default=str))
    return 0


def cmd_list(args) -> int:
    with session_scope() as session:
        rows, total = repo.list_documents(session, limit=args.limit)
        print(f"{total} document(s)")
        for d in rows:
            print(f"  {d.id}  {d.status:22} {d.page_count:>3}p  "
                  f"{(d.company_name or '-')[:24]:24} {d.original_filename[:44]}")
    return 0


def cmd_show(args) -> int:
    from sqlalchemy import select
    from app.database.models import FinancialFactRow
    with session_scope() as session:
        rows = list(session.scalars(
            select(FinancialFactRow)
            .where(FinancialFactRow.document_id == args.document_id)
            .order_by(FinancialFactRow.source_page,
                      FinancialFactRow.canonical_metric)
            .limit(args.limit)))
        if not rows:
            print("no facts")
            return 1
        print(f"{'metric':26} {'entity':14} {'period':12} {'value':>14} "
              f"{'unit':8} {'cur':4} {'growth':>9} {'conf':>5} {'valid':10} p")
        for f in rows:
            g = f"{f.growth:+.1f}%" if f.growth is not None else "-"
            print(f"{f.canonical_metric[:26]:26} {(f.entity or '')[:14]:14} "
                  f"{(f.period or '')[:12]:12} "
                  f"{('' if f.value is None else f'{f.value:,.2f}'):>14} "
                  f"{f.unit[:8]:8} {f.currency[:4]:4} {g:>9} "
                  f"{f.confidence:.2f} {f.validation_status[:10]:10} {f.source_page}")
    return 0


def cmd_export(args) -> int:
    from app.export import exporters
    with session_scope() as session:
        data = exporters.EXPORTERS[args.format](session, args.document_id)
    out = Path(args.output or f"{args.document_id}.{args.format}")
    out.write_bytes(data)
    print(f"wrote {out} ({len(data):,} bytes)")
    return 0


def cmd_evaluate(args) -> int:
    from app.evaluation.runner import run_evaluation
    report = run_evaluation(args.document_id, truth_path=args.truth)
    print(json.dumps(report, indent=2, default=str))
    return 0


def cmd_baseline(args) -> int:
    from app.evaluation.baseline import compare_baseline_vs_visual
    report = compare_baseline_vs_visual(args.path, document_id=args.document_id)
    print(json.dumps(report, indent=2, default=str))
    return 0


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    parser = argparse.ArgumentParser(prog="finsight", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init-db"); p.add_argument("--drop", action="store_true")
    p.set_defaults(func=cmd_init_db)

    p = sub.add_parser("engines"); p.set_defaults(func=cmd_engines)

    p = sub.add_parser("ingest")
    p.add_argument("path")
    p.add_argument("--force-ocr", action="store_true")
    p.add_argument("--allow-duplicate", action="store_true")
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("status"); p.add_argument("document_id")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("list"); p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("show")
    p.add_argument("document_id"); p.add_argument("--limit", type=int, default=40)
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("export")
    p.add_argument("document_id")
    p.add_argument("--format", choices=["json", "csv", "xlsx"], default="json")
    p.add_argument("-o", "--output")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("evaluate")
    p.add_argument("document_id"); p.add_argument("--truth")
    p.set_defaults(func=cmd_evaluate)

    p = sub.add_parser("baseline")
    p.add_argument("path"); p.add_argument("--document-id")
    p.set_defaults(func=cmd_baseline)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
