"""Directory layout constants for daily-run artifact publication.

Single responsibility: name the on-disk layout once so publishers and index
writers cannot drift apart.
"""

from __future__ import annotations

CANONICAL_DIR = "01_canonical"
REPORT_DIR = "02_report"
EXPORT_DIR = "03_exports"
AUDIT_DIR = "04_audit"
LAYOUT_VERSION = "2"
SUMMARY_DIR = "汇总结果"

__all__ = [
    "AUDIT_DIR",
    "CANONICAL_DIR",
    "EXPORT_DIR",
    "LAYOUT_VERSION",
    "REPORT_DIR",
    "SUMMARY_DIR",
]
