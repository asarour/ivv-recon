"""Finding model and severity rules."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class Severity(str, Enum):
    """Severity ordering drives the pass/fail gate and report ordering."""

    CRITICAL = "critical"  # data loss or corruption. Migration must not proceed.
    HIGH = "high"          # material discrepancy. Needs resolution before sign-off.
    MEDIUM = "medium"      # explainable but must be documented.
    LOW = "low"            # informational drift.
    INFO = "info"          # observation, no action.

    @property
    def rank(self) -> int:
        return {
            Severity.CRITICAL: 0,
            Severity.HIGH: 1,
            Severity.MEDIUM: 2,
            Severity.LOW: 3,
            Severity.INFO: 4,
        }[self]


class CheckStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    ERROR = "error"   # the check itself blew up. Never treat as a pass.
    SKIPPED = "skipped"


@dataclass
class Finding:
    """One specific discrepancy."""

    check: str
    severity: Severity
    summary: str
    detail: str = ""
    #: Sample offending rows/keys. Capped -- a report with 4M rows in it is unusable.
    samples: list[dict[str, Any]] = field(default_factory=list)
    sample_truncated: bool = False
    total_affected: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "check": self.check,
            "severity": self.severity.value,
            "summary": self.summary,
            "detail": self.detail,
            "samples": self.samples,
            "sample_truncated": self.sample_truncated,
            "total_affected": self.total_affected,
        }


@dataclass
class CheckResult:
    """Outcome of one check."""

    name: str
    status: CheckStatus
    findings: list[Finding] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    duration_ms: float = 0.0
    error: str | None = None

    @property
    def worst_severity(self) -> Severity | None:
        if not self.findings:
            return None
        return min((f.severity for f in self.findings), key=lambda s: s.rank)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status.value,
            "findings": [f.as_dict() for f in self.findings],
            "metrics": self.metrics,
            "duration_ms": round(self.duration_ms, 1),
            "error": self.error,
        }


@dataclass
class ReconReport:
    """Everything produced by one reconciliation run."""

    source_table: str
    target_table: str
    source_dialect: str
    target_dialect: str
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None
    results: list[CheckResult] = field(default_factory=list)
    #: Severity at or above which the run is considered a failure.
    fail_threshold: Severity = Severity.HIGH

    @property
    def all_findings(self) -> list[Finding]:
        out: list[Finding] = []
        for r in self.results:
            out.extend(r.findings)
        return sorted(out, key=lambda f: f.severity.rank)

    @property
    def errored(self) -> list[CheckResult]:
        return [r for r in self.results if r.status is CheckStatus.ERROR]

    def counts_by_severity(self) -> dict[str, int]:
        counts = {s.value: 0 for s in Severity}
        for f in self.all_findings:
            counts[f.severity.value] += 1
        return counts

    def passed(self) -> bool:
        """A run with an errored check never passes.

        An exception inside a check means we do not know whether the data is good.
        'Unknown' is not 'clean'.
        """
        if self.errored:
            return False
        return not any(
            f.severity.rank <= self.fail_threshold.rank for f in self.all_findings
        )

    def exit_code(self) -> int:
        if self.errored:
            return 2
        return 0 if self.passed() else 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_table": self.source_table,
            "target_table": self.target_table,
            "source_dialect": self.source_dialect,
            "target_dialect": self.target_dialect,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "passed": self.passed(),
            "counts_by_severity": self.counts_by_severity(),
            "results": [r.as_dict() for r in self.results],
        }
