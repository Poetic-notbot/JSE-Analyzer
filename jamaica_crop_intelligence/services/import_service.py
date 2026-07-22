"""Data stewardship service: source files, import runs, quality flags, commit.

This service coordinates the ingestion workflow:

    upload/fetch  ->  register source_file (hash dedupe)
    parse         ->  preview rows + data_quality_flags
    approve       ->  commit normalized rows into fact tables (idempotent by
                      record_key), logging rows_committed / rows_skipped
    reject        ->  mark the import_run rejected, commit nothing
"""

from __future__ import annotations

import hashlib

import pandas as pd

from ..ingestion import ministry_ingestion as ingestion
from .repository import Repository


class ImportService:
    def __init__(self, repo: Repository):
        self.repo = repo

    # ---------------------------------------------------------- source files
    @staticmethod
    def file_hash(content: bytes) -> str:
        return hashlib.sha256(content).hexdigest()

    def find_by_hash(self, file_hash: str) -> dict | None:
        row = self.repo.query_one(
            "SELECT * FROM source_files WHERE file_hash=?", (file_hash,))
        return dict(row) if row else None

    def register_source_file(self, filename: str, content: bytes, kind: str,
                             origin_url: str | None = None,
                             provenance: str = "official_observed") -> tuple[int, bool]:
        """Register a file. Returns (source_file_id, is_new). Deduped by hash."""
        h = self.file_hash(content)
        existing = self.find_by_hash(h)
        if existing:
            return existing["id"], False
        sid = self.repo.insert("source_files", {
            "filename": filename, "file_hash": h, "kind": kind,
            "origin_url": origin_url, "size_bytes": len(content),
            "provenance": provenance,
        }, or_ignore=True)
        if sid == 0:
            existing = self.find_by_hash(h)
            return (existing["id"], False) if existing else (0, False)
        return sid, True

    def source_files(self) -> pd.DataFrame:
        return self.repo.df(
            "SELECT id, filename, kind, provenance, size_bytes, origin_url, uploaded_at "
            "FROM source_files ORDER BY uploaded_at DESC")

    # ----------------------------------------------------------- import runs
    def start_run(self, source_file_id: int, kind: str, rows_parsed: int,
                  message: str = "") -> int:
        return self.repo.insert("import_runs", {
            "source_file_id": source_file_id, "kind": kind,
            "status": "previewed", "rows_parsed": rows_parsed, "message": message,
        })

    def log_flag(self, import_run_id: int, category: str, detail: str,
                 raw_value: str = "", severity: str = "warning") -> None:
        self.repo.insert("data_quality_flags", {
            "import_run_id": import_run_id, "severity": severity,
            "category": category, "detail": detail, "raw_value": raw_value,
        })

    def flags_for_run(self, import_run_id: int) -> pd.DataFrame:
        return self.repo.df(
            "SELECT severity, category, detail, raw_value, resolved "
            "FROM data_quality_flags WHERE import_run_id=? ORDER BY severity",
            (import_run_id,))

    def import_runs(self) -> pd.DataFrame:
        return self.repo.df(
            "SELECT ir.id, sf.filename, ir.kind, ir.status, ir.rows_parsed, "
            "ir.rows_committed, ir.rows_skipped, ir.message, ir.started_at "
            "FROM import_runs ir LEFT JOIN source_files sf ON sf.id=ir.source_file_id "
            "ORDER BY ir.started_at DESC")

    # -------------------------------------------------------------- workflow
    def preview(self, content: bytes, filename: str, kind: str) -> dict:
        """Parse a file into a normalized preview + quality report.

        Returns a dict with a normalized DataFrame, a list of quality issues,
        and crop-name / unit review items. Nothing is committed here.
        """
        return ingestion.parse_file(content, filename, kind, self.repo)

    def commit_preview(self, source_file_id: int, kind: str,
                       normalized: pd.DataFrame,
                       quality_issues: list[dict] | None = None) -> dict:
        """Commit approved normalized rows into fact tables (idempotent)."""
        run_id = self.start_run(source_file_id, kind, len(normalized),
                                message="approved")
        for issue in (quality_issues or []):
            self.log_flag(run_id, issue.get("category", "parse"),
                          issue.get("detail", ""), issue.get("raw_value", ""),
                          issue.get("severity", "warning"))

        committed = ingestion.commit_rows(
            normalized, kind, source_file_id, self.repo)

        skipped = len(normalized) - committed
        self.repo.execute(
            "UPDATE import_runs SET status='committed', rows_committed=?, "
            "rows_skipped=?, finished_at=datetime('now') WHERE id=?",
            (committed, skipped, run_id))
        return {"import_run_id": run_id, "rows_committed": committed,
                "rows_skipped": skipped}

    def reject_preview(self, source_file_id: int, kind: str, rows_parsed: int,
                       reason: str) -> int:
        run_id = self.start_run(source_file_id, kind, rows_parsed,
                                message=f"rejected: {reason}")
        self.repo.execute(
            "UPDATE import_runs SET status='rejected', "
            "finished_at=datetime('now') WHERE id=?", (run_id,))
        return run_id
