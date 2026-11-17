"""Frozen engineering limits for the course resource centre.

These are Openthu engineering limits, not school rules.  They bound memory,
terminal output and one batch request; a stricter platform contract may only
lower them.  The index is decrypted into memory for search, so the entry and
byte ceilings are the main guard against an unbounded personal coursework
database.
"""
from __future__ import annotations

from dataclasses import dataclass, fields, replace


@dataclass(frozen=True)
class ResourceLimits:
    """One immutable limits object shared by every entry point."""

    max_entries_per_account: int = 5000
    max_entries_per_semester: int = 2000
    max_body_chars: int = 2000
    max_title_chars: int = 240
    max_index_bytes: int = 4 * 1024 * 1024
    max_search_limit: int = 50
    default_search_limit: int = 20
    max_manifest_limit: int = 500
    default_manifest_limit: int = 200
    max_download_record_limit: int = 500
    default_download_record_limit: int = 100
    max_plan_items: int = 200
    max_plan_known_bytes: int = 8 * 1024 * 1024 * 1024
    max_preview_hash_bytes: int = 512 * 1024 * 1024
    max_pending_plans: int = 20
    max_export_items: int = 5000
    max_attachment_bytes: int = 128 * 1024 * 1024
    max_detail_reads: int = 200
    max_courses_per_read: int = 200
    max_file_name_bytes: int = 200
    max_relative_path_bytes: int = 240
    media_reference_ttl_seconds: int = 86400
    retention_days: int = 180
    check_history: int = 20
    plan_ttl_seconds: int = 600

    def __post_init__(self) -> None:
        for item in fields(self):
            value = getattr(self, item.name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{item.name} must be a positive integer")
        if self.default_search_limit > self.max_search_limit:
            raise ValueError("default_search_limit must not exceed max_search_limit")

    def stricter(self, platform: dict[str, object]) -> "ResourceLimits":
        """Adopt a platform contract only where it is stricter."""
        known = {item.name for item in fields(self)}
        unknown = sorted(set(platform) - known)
        if unknown:
            # A misspelt limit must not silently keep the looser local value.
            raise ValueError(f"unknown resource limit {unknown[0]}")
        changes = {}
        for key, value in platform.items():
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"platform limit {key} must be a positive integer")
            changes[key] = min(getattr(self, key), value)
        if not changes:
            return self
        if "max_search_limit" in changes:
            changes["default_search_limit"] = min(
                changes["max_search_limit"], changes.get("default_search_limit", self.default_search_limit)
            )
        return replace(self, **changes)


DEFAULT_LIMITS = ResourceLimits()

__all__ = ["DEFAULT_LIMITS", "ResourceLimits"]
