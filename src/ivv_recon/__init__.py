"""ivv-recon: source-to-target reconciliation for ETL migrations.

Validates that data which left a source system arrived intact in a target,
across engines that cannot see each other (SQL Server, Azure SQL, Azure Synapse,
Snowflake).
"""

__version__ = "0.1.0"

from .findings import CheckStatus, Finding, ReconReport, Severity
from .schema import Column, ColumnMapping, LogicalType, MappingSpec, TableRef

__all__ = [
    "__version__",
    "Column",
    "ColumnMapping",
    "LogicalType",
    "MappingSpec",
    "TableRef",
    "Finding",
    "ReconReport",
    "Severity",
    "CheckStatus",
]
