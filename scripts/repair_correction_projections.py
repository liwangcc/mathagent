"""Repair legacy question-level correction projections.

The command is deliberately dry-run by default.  Use ``--apply`` only after
reviewing the metadata-only report.  It never calls a model and never prints
student answers, images, or credentials.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database import SessionLocal  # noqa: E402
from services.correction_tracking import repair_missing_question_projections  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Repair missing question correction projections")
    parser.add_argument("--student-id", type=int, default=None)
    parser.add_argument("--grading-result-id", type=int, default=None)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write eligible projections; omitted means a read-only dry run",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    db = SessionLocal()
    try:
        summary = repair_missing_question_projections(
            db,
            student_id=args.student_id,
            grading_result_id=args.grading_result_id,
            dry_run=not args.apply,
        )
        if args.apply:
            db.commit()
        # Keep the operator report bounded to counts and state metadata.
        safe_reports = [
            {
                key: report.get(key)
                for key in (
                    "grading_result_id",
                    "status",
                    "skip_reason",
                    "declared_total_questions",
                    "declared_wrong_count",
                    "detail_count",
                    "expected_question_count",
                    "attempt_count",
                    "cycle_count",
                    "cycle_statuses",
                    "wrong_attempts_missing_cycle",
                    "inconsistent_attempts",
                    "invalid_attempts",
                    "result_mismatches",
                    "attempt_statuses",
                    "projection_complete",
                    "repairable",
                    "created_attempts",
                    "would_create_attempts",
                    "would_repair_cycles",
                )
                if key in report
            }
            for report in summary.get("reports", [])
        ]
        output = {key: value for key, value in summary.items() if key != "reports"}
        output["reports"] = safe_reports
        print(json.dumps(output, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as exc:  # pragma: no cover - command-line safety net
        db.rollback()
        print(f"repair failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
