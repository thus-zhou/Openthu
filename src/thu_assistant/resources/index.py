"""Encrypted, account- and semester-scoped resource index.

The index holds metadata for course files, notice attachments and assignment
attachments.  It never stores file bytes and never stores an absolute path.
Persistence goes through the 2.2 protected store under the ``INDEX`` purpose;
search decrypts a bounded document into memory and never writes a plaintext
full-text database.

Version rules (P0-1/P1-1/P1-9):

* a reliable source update instant is the version, otherwise an entry gets a
  bounded ``u<n>`` sequence token;
* a known source version older than the active one never replaces it; the
  older observation is kept as history and a warning is recorded;
* a content-derived ``c:<hash>`` version remembers its ``base_version``.  A
  same-base refresh keeps the content version active; only a strictly newer
  source version may supersede it;
* content bytes are committed to the index only after the archive write
  succeeded, and the in-memory change is rolled back if the protected save
  fails.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable

from ..source_state import normalize_timestamp, now_iso
from .errors import (
    ResourceAccountMismatch,
    ResourceIndexCorrupt,
    ResourceInvalidArgument,
    ResourceLimitExceeded,
    ResourceNotFound,
    ResourceStoreUnavailable,
    ResourceTitleUnsafe,
    ResourceVersionConflict,
    require_bool,
)
from .identity import SOURCE_KINDS, validate_identifier, validate_semester
from .limits import DEFAULT_LIMITS, ResourceLimits
from .model import DownloadRecord, ResourceCandidate, ResourceEntry, empty_index

INDEX_VERSION = "resource-index-v1"
SEARCH_VERSION = "resource-search-v1"
TZ = timezone(timedelta(hours=8))
_CODE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{2,80}$")
_STORE_ERRORS = {
    "SecureRecordNotFound": "not_found",
    "FileNotFoundError": "not_found",
    "SecureStoreBusy": "busy",
    "SecureKeyUnavailable": "key_unavailable",
    "SecureKeyNotConfigured": "key_not_configured",
    "SecureKeyMismatch": "key_mismatch",
    "SecureStoreCorrupt": "corrupt",
    "SecurePayloadInvalid": "payload_invalid",
    "SecureEnvelopeCorrupt": "envelope_corrupt",
    "SecureDecryptError": "decrypt_failed",
    "SecureWriteError": "write_failed",
    "SecureAccountMismatch": "account_mismatch",
    "SecureRevisionConflict": "revision_conflict",
}
_CORRUPT_DETAILS = {"payload_invalid", "corrupt", "envelope_corrupt", "decrypt_failed"}


def clean_codes(values: Any) -> list[str]:
    """Keep only stable code-like strings; never persist free text."""
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]
    out = set()
    for item in values:
        code = item.get("code") if isinstance(item, dict) else item
        if isinstance(code, str) and _CODE.fullmatch(code):
            out.add(code)
    return sorted(out)


def default_clock() -> datetime:
    return datetime.now(TZ)


def destination_id(destination: str) -> str:
    digest = hashlib.sha256(destination.encode("utf-8", "replace")).hexdigest()[:12]
    return "dst_" + digest


def _parse_time(value: str | None) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=TZ)


class ResourceIndex:
    """One account's index for one semester."""

    def __init__(
        self,
        store: Any,
        *,
        account: str,
        account_scope: str,
        semester: str,
        limits: ResourceLimits = DEFAULT_LIMITS,
        clock: Callable[[], datetime] = default_clock,
    ) -> None:
        if store is None or not hasattr(store, "write_json") or not hasattr(store, "read_json"):
            raise ResourceInvalidArgument("store")
        if not isinstance(account, str) or not account:
            raise ResourceInvalidArgument("account")
        if not isinstance(account_scope, str) or not account_scope.startswith("acct_"):
            raise ResourceInvalidArgument("account_scope_format")
        self.store = store
        self.account = account
        self.account_scope = account_scope
        self.semester = validate_semester(semester)
        self.limits = limits
        self.clock = clock
        self.entries: list[ResourceEntry] = []
        self.records: list[DownloadRecord] = []
        self.checks: list[dict[str, Any]] = []
        self.updated_at: str | None = None
        self._loaded = False

    # -- lifecycle ----------------------------------------------------
    @property
    def record_id(self) -> str:
        return f"res-{self.account_scope[5:29]}-{self.semester}"

    def _store_call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        try:
            return getattr(self.store, method)(*args, **kwargs)
        except Exception as error:
            name = type(error).__name__
            if name in _STORE_ERRORS:
                if name == "SecureRecordNotFound":
                    raise
                raise ResourceStoreUnavailable(_STORE_ERRORS[name]) from None
            if isinstance(error, FileNotFoundError):
                raise
            # Do not hide a genuine implementation bug (TypeError, KeyError)
            # behind a storage category.
            raise

    def load(self) -> "ResourceIndex":
        if self._loaded:
            return self
        try:
            payload = self._store_call(
                "read_json",
                self.record_id,
                purpose="INDEX",
                account_domain="CAMPUS",
                service_domain="LEARN",
                account=self.account,
            )
        except ResourceStoreUnavailable as error:
            if error.detail in _CORRUPT_DETAILS:
                raise ResourceIndexCorrupt(error.detail) from None
            raise
        except Exception as error:
            if type(error).__name__ in ("SecureRecordNotFound", "FileNotFoundError"):
                payload = empty_index(self.account_scope, self.semester)
            else:
                raise
        self._hydrate(payload)
        self._loaded = True
        return self

    def _hydrate(self, payload: Any) -> None:
        if not isinstance(payload, dict) or payload.get("format_version") != INDEX_VERSION:
            raise ResourceIndexCorrupt("format")
        if payload.get("account_scope") != self.account_scope:
            raise ResourceAccountMismatch("scope")
        if payload.get("semester") != self.semester:
            raise ResourceIndexCorrupt("semester")
        entries = payload.get("entries")
        records = payload.get("downloads")
        checks = payload.get("checks")
        if not isinstance(entries, list) or not isinstance(records, list) or not isinstance(checks, list):
            raise ResourceIndexCorrupt("shape")
        try:
            self.entries = [ResourceEntry.from_state(item) for item in entries]
            self.records = [DownloadRecord.from_state(item) for item in records]
        except (ResourceInvalidArgument, ResourceTitleUnsafe):
            raise ResourceIndexCorrupt("entry") from None
        self.checks = [item for item in checks[-self.limits.check_history :] if isinstance(item, dict)]
        self.updated_at = normalize_timestamp(payload.get("updated_at"), fallback=None)
        if any(entry.account_scope != self.account_scope for entry in self.entries):
            raise ResourceAccountMismatch("entry_scope")
        if any(entry.semester != self.semester for entry in self.entries):
            raise ResourceIndexCorrupt("entry_semester")
        self._prune()
        keys = [(entry.stable_resource_uid, entry.version) for entry in self.entries]
        if len(keys) != len(set(keys)):
            raise ResourceIndexCorrupt("duplicate_key")

    def _prune(self) -> None:
        cutoff = self.clock() - timedelta(days=self.limits.retention_days)
        keep = []
        for entry in self.entries:
            checked = _parse_time(entry.last_checked_at)
            if checked is None or checked >= cutoff:
                keep.append(entry)
        self.entries = keep

    def _payload(self) -> dict[str, Any]:
        return {
            "format_version": INDEX_VERSION,
            "account_scope": self.account_scope,
            "semester": self.semester,
            "entries": [entry.to_state() for entry in self.entries],
            "downloads": [record.to_state() for record in self.records],
            "checks": self.checks[-self.limits.check_history :],
            "updated_at": now_iso(self.clock),
        }

    def _write_payload(self, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if len(raw) > self.limits.max_index_bytes:
            raise ResourceLimitExceeded("index_bytes")
        try:
            self._store_call(
                "write_json",
                self.record_id,
                payload,
                purpose="INDEX",
                account_domain="CAMPUS",
                service_domain="LEARN",
                account=self.account,
            )
        except ResourceStoreUnavailable:
            raise
        except Exception as error:
            name = type(error).__name__
            raise ResourceStoreUnavailable(_STORE_ERRORS.get(name, "store_error")) from None

    def save(self) -> None:
        self._prune()
        if len(self.entries) > self.limits.max_entries_per_semester:
            raise ResourceLimitExceeded("entries_per_semester")
        payload = self._payload()
        self._write_payload(payload)
        self.updated_at = payload["updated_at"]

    def delete(self) -> None:
        """Replace the encrypted index with an empty one, then clear memory.

        The 2.2 store has no delete verb; an atomic replacement removes the old
        ciphertext.  Memory is cleared only after the write succeeded, so a
        failed delete leaves the previous index readable.
        """
        payload = empty_index(self.account_scope, self.semester)
        payload["updated_at"] = now_iso(self.clock)
        self._write_payload(payload)
        self.entries = []
        self.records = []
        self.checks = []
        self.updated_at = payload["updated_at"]
        self._loaded = True

    # -- merging observations -----------------------------------------
    def _same_uid(self, uid: str) -> list[ResourceEntry]:
        return [entry for entry in self.entries if entry.stable_resource_uid == uid]

    def _active(self, uid: str) -> ResourceEntry | None:
        return next((entry for entry in self._same_uid(uid) if entry.state == "ACTIVE"), None)

    def _find_exact(self, uid: str, version: str) -> ResourceEntry | None:
        return next(
            (entry for entry in self._same_uid(uid) if entry.version == version),
            None,
        )

    def _next_unknown_version(self, same: list[ResourceEntry]) -> str:
        highest = 0
        for entry in same:
            if entry.version.startswith("u") and entry.version[1:].isdigit():
                highest = max(highest, int(entry.version[1:]))
        return f"u{highest + 1}"

    @staticmethod
    def _content_version(entry: ResourceEntry | None) -> bool:
        return bool(entry and entry.version.startswith("c:") and entry.base_version)

    def _decide(self, active: ResourceEntry | None, candidate: ResourceCandidate) -> tuple[str, str]:
        """Return ``(decision, warning)`` for one observation against the active row."""
        if active is None:
            return "activate", ""
        if candidate.version == "UNKNOWN":
            note = (
                "VERSION_RETAINED_SOURCE_UNRELIABLE"
                if active.version != "UNKNOWN"
                else (active.version_note or "UNKNOWN_SEQUENCE")
            )
            return "refresh_active", note
        if candidate.version == active.version:
            return "refresh_active", ""
        if self._content_version(active):
            if active.base_version == candidate.version:
                return "refresh_content", "CONTENT_VERSION_RETAINED"
            base_time = _parse_time(active.base_version)
            cand_time = _parse_time(candidate.updated_at)
            if base_time is not None and cand_time is not None and cand_time < base_time:
                return "historical", "OLDER_SOURCE_VERSION_IGNORED"
            if base_time is not None and cand_time is not None and cand_time > base_time:
                return "activate", "SOURCE_VERSION_SUPERSEDES_CONTENT_VERSION"
            return "refresh_content", "CONTENT_VERSION_RETAINED"
        active_time = _parse_time(active.updated_at)
        cand_time = _parse_time(candidate.updated_at)
        if active_time is not None and cand_time is not None:
            if cand_time < active_time:
                return "historical", "OLDER_SOURCE_VERSION_IGNORED"
            if cand_time > active_time:
                return "activate", ""
            return "refresh_active", ""
        if active.version.startswith("u") and active.version != "UNKNOWN":
            return "activate", ""
        # A known version without a usable time cannot prove it is newer.
        return "refresh_active", "VERSION_RETAINED_SOURCE_UNRELIABLE"

    def _make_room(self) -> None:
        """Drop the oldest non-active version when the semester cap is reached."""
        while len(self.entries) >= self.limits.max_entries_per_semester:
            removable = [entry for entry in self.entries if entry.state != "ACTIVE"]
            if not removable:
                raise ResourceLimitExceeded("entries_per_semester")
            oldest = min(removable, key=lambda entry: entry.last_checked_at)
            self.entries = [entry for entry in self.entries if entry is not oldest]

    def merge(
        self,
        candidates: Iterable[ResourceCandidate],
        *,
        checked_at: str,
        complete: bool = True,
        source_errors: Iterable[str] = (),
        warnings: Iterable[str] = (),
        mark_absent: bool = False,
        stale_courses: Iterable[str] = (),
    ) -> dict[str, Any]:
        require_bool(complete, "complete")
        require_bool(mark_absent, "mark_absent")
        if not self._loaded:
            self.load()
        checked = normalize_timestamp(checked_at, fallback=None)
        if checked is None:
            raise ResourceInvalidArgument("checked_at_format")
        rows = list(candidates)
        for candidate in rows:
            if not isinstance(candidate, ResourceCandidate):
                raise ResourceInvalidArgument("candidate_type")
            if candidate.semester != self.semester:
                raise ResourceInvalidArgument("candidate_semester")
        unique: dict[tuple[str, str], ResourceCandidate] = {}
        seen_warnings = set(clean_codes(warnings))
        for candidate in rows:
            key = candidate.key()
            previous = unique.get(key)
            if previous is None:
                unique[key] = candidate
                continue
            if (previous.title, previous.size_bytes, previous.updated_at) != (
                candidate.title,
                candidate.size_bytes,
                candidate.updated_at,
            ):
                raise ResourceVersionConflict("duplicate_observation")
            if candidate.body_state == "READ":
                unique[key] = candidate
        stats = {"added": 0, "updated": 0, "superseded": 0, "historical": 0, "warnings": []}
        seen_uids: set[str] = set()
        for (_uid, _version), candidate in unique.items():
            uid = candidate.stable_resource_uid
            seen_uids.add(uid)
            active = self._active(uid)
            decision, note = self._decide(active, candidate)
            if note and note not in stats["warnings"]:
                stats["warnings"].append(note)
            if decision == "refresh_active" and active is not None:
                refreshed = active.refreshed(candidate, checked_at=checked)
                if note:
                    refreshed = replace(refreshed, version_note=note)
                self._replace(refreshed)
                stats["updated"] += 1
                continue
            if decision == "refresh_content" and active is not None:
                refreshed = active.refreshed(candidate, checked_at=checked)
                self._replace(refreshed)
                stats["updated"] += 1
                continue
            if decision == "historical":
                existing = self._find_exact(uid, candidate.version)
                if existing is not None:
                    refreshed = existing.refreshed(candidate, checked_at=checked)
                    self._replace(replace(refreshed, state="SUPERSEDED"))
                    stats["updated"] += 1
                else:
                    self._make_room()
                    self.entries.append(
                        ResourceEntry.from_candidate(
                            candidate,
                            account_scope=self.account_scope,
                            checked_at=checked,
                            state="SUPERSEDED",
                            version_note=note,
                        )
                    )
                    stats["historical"] += 1
                continue
            target_version = candidate.version
            if target_version == "UNKNOWN":
                target_version = self._next_unknown_version(self._same_uid(uid))
            existing = self._find_exact(uid, target_version)
            if existing is not None:
                refreshed = existing.refreshed(candidate, checked_at=checked)
                if note:
                    refreshed = replace(refreshed, version_note=note)
                self._replace(refreshed)
                stats["updated"] += 1
            else:
                self._make_room()
                self.entries.append(
                    ResourceEntry.from_candidate(
                        candidate,
                        account_scope=self.account_scope,
                        checked_at=checked,
                        version=target_version,
                        version_note=note,
                    )
                )
                stats["added"] += 1
            for entry in list(self.entries):
                if (
                    entry.stable_resource_uid == uid
                    and entry.version != target_version
                    and entry.state == "ACTIVE"
                ):
                    self._replace(replace(entry, state="SUPERSEDED"))
                    stats["superseded"] += 1
        stale_set = {item for item in stale_courses if isinstance(item, str) and item}
        if mark_absent and complete:
            changed = 0
            for entry in list(self.entries):
                if entry.state == "ACTIVE" and entry.stable_resource_uid not in seen_uids:
                    self._replace(replace(entry, state="MISSING"))
                    changed += 1
            stats["missing"] = changed
        elif stale_set:
            changed = 0
            for entry in list(self.entries):
                if (
                    entry.state == "ACTIVE"
                    and entry.stable_resource_uid not in seen_uids
                    and entry.course_id in stale_set
                ):
                    self._replace(replace(entry, state="STALE"))
                    changed += 1
            stats["stale"] = changed
        self.checks.append(
            {
                "checked_at": checked,
                "complete": bool(complete),
                "candidates": len(unique),
                "source_errors": clean_codes(source_errors),
                "warnings": clean_codes(seen_warnings | set(stats["warnings"])),
                "added": stats["added"],
                "updated": stats["updated"],
                "superseded": stats["superseded"],
                "historical": stats["historical"],
                "missing": stats.get("missing", 0),
                "stale": stats.get("stale", 0),
            }
        )
        self.checks = self.checks[-self.limits.check_history :]
        self.save()
        return stats

    def _replace(self, entry: ResourceEntry) -> None:
        for index, current in enumerate(self.entries):
            if (
                current.stable_resource_uid == entry.stable_resource_uid
                and current.version == entry.version
            ):
                self.entries[index] = entry
                return
        self.entries.append(entry)

    # -- content commits ------------------------------------------------
    def content_target(
        self,
        entry: ResourceEntry,
        content_sha256: str,
        *,
        checked_at: str,
    ) -> tuple[ResourceEntry, bool]:
        """Compute the entry for downloaded bytes without mutating the index."""
        if not self._loaded:
            self.load()
        if len(content_sha256) != 64:
            raise ResourceInvalidArgument("content_sha256_format")
        if entry.state != "ACTIVE":
            # Writing new bytes under an old version token would make history
            # claim the old hash while the file holds new content.  A
            # historical entry that has never recorded a hash may record the
            # first downloaded bytes; a different hash is refused.
            if entry.content_sha256 is None:
                return replace(entry, content_sha256=content_sha256), False
            if entry.content_sha256 == content_sha256:
                return entry, False
            raise ResourceVersionConflict("historical_content_changed")
        if entry.content_sha256 == content_sha256:
            return entry, False
        if entry.content_sha256 is None:
            return replace(entry, content_sha256=content_sha256), False
        new_version = "c:" + content_sha256[:12]
        existing = self._find_exact(entry.stable_resource_uid, new_version)
        if existing is not None:
            return replace(
                existing,
                content_sha256=content_sha256,
                last_checked_at=checked_at,
                state="ACTIVE",
            ), True
        if len(self.entries) >= self.limits.max_entries_per_semester and not any(
            item.state != "ACTIVE" for item in self.entries
        ):
            raise ResourceLimitExceeded("entries_per_semester")
        return replace(
            entry,
            version=new_version,
            base_version=entry.version,
            content_sha256=content_sha256,
            first_checked_at=checked_at,
            last_checked_at=checked_at,
            state="ACTIVE",
            version_note="CONTENT_CHANGED_UNDER_SAME_VERSION",
        ), True

    def commit_content(self, entry: ResourceEntry, *, bumped: bool) -> None:
        """Apply a computed content target after the archive write succeeded."""
        require_bool(bumped, "bumped")
        snapshot_entries = list(self.entries)
        snapshot_records = list(self.records)
        try:
            if bumped:
                self._make_room()
                for current in list(self.entries):
                    if (
                        current.stable_resource_uid == entry.stable_resource_uid
                        and current.version != entry.version
                        and current.state == "ACTIVE"
                    ):
                        self._replace(replace(current, state="SUPERSEDED"))
            self._replace(entry)
            self.save()
        except BaseException:
            self.entries = snapshot_entries
            self.records = snapshot_records
            raise

    def apply_content_hash(
        self,
        entry: ResourceEntry,
        content_sha256: str,
        *,
        checked_at: str,
    ) -> tuple[ResourceEntry, bool]:
        """Compatibility wrapper: compute then commit in one call."""
        target, bumped = self.content_target(entry, content_sha256, checked_at=checked_at)
        self.commit_content(target, bumped=bumped)
        return target, bumped

    # -- search --------------------------------------------------------
    def coverage(self) -> dict[str, Any]:
        if not self._loaded:
            self.load()
        by_source = {source: 0 for source in SOURCE_KINDS}
        body = {"READ": 0, "NOT_READ": 0, "UNRESOLVED": 0, "UNKNOWN": 0, "TRUNCATED": 0}
        states = {state: 0 for state in ("ACTIVE", "SUPERSEDED", "STALE", "MISSING")}
        for entry in self.entries:
            by_source[entry.source] = by_source.get(entry.source, 0) + 1
            body[entry.body_state] = body.get(entry.body_state, 0) + 1
            if entry.body_truncated:
                body["TRUNCATED"] += 1
            states[entry.state] = states.get(entry.state, 0) + 1
        last = self.checks[-1] if self.checks else None
        complete = bool(last and last.get("complete") and not last.get("source_errors"))
        body_complete = (
            body["NOT_READ"] == 0
            and body["UNRESOLVED"] == 0
            and body["UNKNOWN"] == 0
            and body["TRUNCATED"] == 0
        )
        return {
            "semester": self.semester,
            "entries": len(self.entries),
            "states": states,
            "by_source": by_source,
            "body": body | {"searchable": body["READ"] > 0, "complete": body_complete},
            "updated_time_unknown": sum(1 for entry in self.entries if entry.updated_at is None),
            "content_hash_known": sum(1 for entry in self.entries if entry.content_sha256),
            "checked_at": (last or {}).get("checked_at"),
            "complete": complete,
            "source_errors": list((last or {}).get("source_errors") or []),
            "warnings": list((last or {}).get("warnings") or []),
            "historical": bool(last and not last.get("complete")),
            "index_updated_at": self.updated_at,
        }

    def search(
        self,
        *,
        query: str | None = None,
        course_id: str | None = None,
        source: str | None = None,
        body_state: str | None = None,
        updated_after: str | None = None,
        updated_before: str | None = None,
        state: str | None = None,
        include_superseded: bool = False,
        allow_historical: bool = False,
        offset: int = 0,
        limit: int | None = None,
    ) -> dict[str, Any]:
        if not self._loaded:
            self.load()
        require_bool(include_superseded, "include_superseded")
        require_bool(allow_historical, "allow_historical")
        if query is not None and not isinstance(query, str):
            raise ResourceInvalidArgument("query_type")
        text = (query or "").strip().casefold()
        if len(text) > 200:
            raise ResourceInvalidArgument("query_too_long")
        if course_id is not None:
            validate_identifier(course_id, name="course_id")
        if source is not None and source not in SOURCE_KINDS:
            raise ResourceInvalidArgument("source_filter")
        if body_state is not None and body_state not in ("READ", "NOT_READ", "UNRESOLVED", "UNKNOWN"):
            raise ResourceInvalidArgument("body_state_filter")
        if state is not None and state not in ("ACTIVE", "SUPERSEDED", "STALE", "MISSING"):
            raise ResourceInvalidArgument("state_filter")
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise ResourceInvalidArgument("offset")
        size = self.limits.default_search_limit if limit is None else limit
        if not isinstance(size, int) or isinstance(size, bool) or not 1 <= size <= self.limits.max_search_limit:
            raise ResourceInvalidArgument("limit")
        after = normalize_timestamp(updated_after, fallback=None) if updated_after else None
        before = normalize_timestamp(updated_before, fallback=None) if updated_before else None
        if updated_after and after is None:
            raise ResourceInvalidArgument("updated_after_format")
        if updated_before and before is None:
            raise ResourceInvalidArgument("updated_before_format")
        rows: list[tuple[ResourceEntry, list[str]]] = []
        for entry in self.entries:
            if state is not None and entry.state != state:
                continue
            if entry.state == "SUPERSEDED" and not include_superseded:
                continue
            if entry.state == "MISSING" and not include_superseded:
                continue
            if entry.state == "STALE" and not allow_historical:
                continue
            if course_id is not None and entry.course_id != course_id:
                continue
            if source is not None and entry.source != source:
                continue
            if body_state is not None and entry.body_state != body_state:
                continue
            if after is not None and (entry.updated_at is None or entry.updated_at < after):
                continue
            if before is not None and (entry.updated_at is None or entry.updated_at > before):
                continue
            matched: list[str] = []
            if text:
                if text in entry.title.casefold():
                    matched.append("title")
                if text in entry.course_name.casefold():
                    matched.append("course_name")
                if text in entry.source.casefold():
                    matched.append("source")
                if text in entry.source_record_id.casefold():
                    matched.append("source_record_id")
                if entry.body_state == "READ" and entry.body_excerpt and text in entry.body_excerpt.casefold():
                    matched.append("body_excerpt")
                if not matched:
                    continue
            else:
                matched.append("filters")
            rows.append((entry, matched))
        rows.sort(key=lambda item: (item[0].title.casefold(), item[0].stable_resource_uid))
        rows.sort(key=lambda item: item[0].updated_at or "", reverse=True)
        total = len(rows)
        page = rows[offset : offset + size]
        coverage = self.coverage()
        warnings = set(coverage["warnings"])
        truncated = coverage["body"]["TRUNCATED"]
        if text and truncated:
            warnings.add("BODY_TEXT_COVERAGE_TRUNCATED")
        if text and coverage["body"]["READ"] < coverage["entries"]:
            warnings.add("BODY_TEXT_COVERAGE_PARTIAL")
        snapshot_complete = offset + len(page) >= total
        body_ok = not text or truncated == 0
        complete = bool(coverage["complete"] and snapshot_complete and body_ok)
        status = "PARTIAL" if not complete else ("EMPTY" if total == 0 else "OK")
        checked_at = coverage["checked_at"] or self.updated_at
        return {
            "format_version": SEARCH_VERSION,
            "semester": self.semester,
            "query": query,
            "status": status,
            "items": [
                entry.public()
                | {
                    "matched_fields": matched,
                    "historical": entry.state == "STALE",
                }
                for entry, matched in page
            ],
            "total": total,
            "total_known": coverage["complete"],
            "offset": offset,
            "limit": size,
            "next_offset": offset + len(page) if offset + len(page) < total else None,
            "snapshot_complete": snapshot_complete,
            "source_complete": coverage["complete"],
            "complete": complete,
            "checked_at": checked_at,
            "body_coverage": {
                "read": coverage["body"]["READ"],
                "not_read": coverage["body"]["NOT_READ"],
                "unresolved": coverage["body"]["UNRESOLVED"],
                "unknown": coverage["body"]["UNKNOWN"],
                "truncated": truncated,
                "searchable": coverage["body"]["searchable"],
                "complete": coverage["body"]["complete"],
            },
            "warnings": sorted(warnings),
            "source_errors": coverage["source_errors"],
        }

    # -- reads ---------------------------------------------------------
    def entries_for(self, semester: str | None = None) -> list[ResourceEntry]:
        if semester is not None and semester != self.semester:
            return []
        if not self._loaded:
            self.load()
        return list(self.entries)

    def find(
        self,
        stable_resource_uid: str,
        *,
        version: str | None = None,
        include_superseded: bool = False,
        allow_historical: bool = False,
    ) -> ResourceEntry:
        require_bool(include_superseded, "include_superseded")
        require_bool(allow_historical, "allow_historical")
        if not self._loaded:
            self.load()
        matches = [entry for entry in self.entries if entry.stable_resource_uid == stable_resource_uid]
        if not matches:
            raise ResourceNotFound("uid")
        if version is not None:
            exact = [entry for entry in matches if entry.version == version]
            if not exact:
                raise ResourceNotFound("version")
            entry = exact[0]
        else:
            active = [entry for entry in matches if entry.state == "ACTIVE"]
            if not active and allow_historical:
                active = [entry for entry in matches if entry.state == "STALE"]
            if not active:
                raise ResourceNotFound("active_version")
            entry = sorted(active, key=lambda item: item.last_checked_at)[-1]
        if entry.state == "STALE" and not allow_historical:
            raise ResourceNotFound("stale")
        if entry.state in ("SUPERSEDED", "MISSING") and not include_superseded:
            raise ResourceNotFound("state")
        return entry

    def downloads(self, *, stable_resource_uid: str | None = None) -> list[DownloadRecord]:
        if not self._loaded:
            self.load()
        rows = [
            record
            for record in self.records
            if stable_resource_uid is None or record.stable_resource_uid == stable_resource_uid
        ]
        rows.sort(key=lambda record: record.written_at, reverse=True)
        return rows

    def add_download(self, record: DownloadRecord) -> None:
        if not self._loaded:
            self.load()
        if record.stable_resource_uid not in {entry.stable_resource_uid for entry in self.entries}:
            raise ResourceNotFound("download_uid")
        snapshot = list(self.records)
        try:
            self.records.append(record)
            if len(self.records) > self.limits.max_entries_per_account:
                self.records = self.records[-self.limits.max_entries_per_account :]
            self.save()
        except BaseException:
            self.records = snapshot
            raise


__all__ = [
    "INDEX_VERSION",
    "SEARCH_VERSION",
    "ResourceIndex",
    "clean_codes",
    "default_clock",
    "destination_id",
]
