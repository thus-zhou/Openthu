"""Public facade for the course resource centre (module 4.2).

The facade is deliberately not registered in ``queries.py`` or the MCP tool
list: the business facade (6.1) owns that wiring.  Everything here is a local,
account-scoped operation over an encrypted index plus an explicit batch
download that calls the existing course download interface through
``LearnDownloader``.

Nothing runs implicitly.  Indexing never downloads, a preview never downloads,
and execution requires ``confirm=True`` for that plan.  Plans are process-local
and expire; a restart requires a new preview.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Iterable

from ..source_state import now_iso
from . import sources
from .archive import Archive
from .errors import (
    ResourceError,
    ResourceIndexIncomplete,
    ResourceInvalidArgument,
    ResourcePlanExpired,
    ResourceStoreUnavailable,
)
from .export import export_summary as _export_summary
from .identity import validate_semester
from .index import ResourceIndex, clean_codes, default_clock
from .limits import DEFAULT_LIMITS, ResourceLimits
from .manifest import build_manifest
from .model import ResourceCandidate, ResourceEntry
from .plan import DownloadPlan, build_plan, execute_plan

FACADE_VERSION = "resource-centre-v1"


def _selection(value: Any) -> tuple[str, str | None]:
    if isinstance(value, str):
        return value, None
    if isinstance(value, dict):
        uid = value.get("stable_resource_uid")
        version = value.get("version")
        if version is not None and not isinstance(version, str):
            raise ResourceInvalidArgument("selection_version")
        if not isinstance(uid, str):
            raise ResourceInvalidArgument("selection_uid")
        return uid, version
    raise ResourceInvalidArgument("selection")


class ResourceCenter:
    """The only supported entry point for the local resource centre."""

    def __init__(
        self,
        store: Any,
        account: str,
        *,
        limits: ResourceLimits = DEFAULT_LIMITS,
        clock: Callable[[], datetime] = default_clock,
        media_assets: Any = None,
        fetcher: Any = None,
    ) -> None:
        if store is None or not hasattr(store, "account_scope"):
            raise ResourceInvalidArgument("store")
        if not isinstance(account, str) or not account:
            raise ResourceInvalidArgument("account")
        if not isinstance(limits, ResourceLimits):
            raise ResourceInvalidArgument("limits")
        if media_assets is not None and not hasattr(media_assets, "register_downloaded_attachment"):
            raise ResourceInvalidArgument("media_assets")
        self.store = store
        self.account = account
        self.limits = limits
        self.clock = clock
        self.media_assets = media_assets
        self.fetcher = fetcher
        scope = store.account_scope(
            account,
            purpose="INDEX",
            account_domain="CAMPUS",
            service_domain="LEARN",
        )
        if not isinstance(scope, str) or not scope.startswith("acct_"):
            raise ResourceInvalidArgument("account_scope")
        self.account_scope = scope
        self._indices: dict[str, ResourceIndex] = {}
        self._plans: dict[str, DownloadPlan] = {}

    # -- index ---------------------------------------------------------
    def index(self, semester: str) -> ResourceIndex:
        semester = validate_semester(semester)
        if semester not in self._indices:
            self._indices[semester] = ResourceIndex(
                self.store,
                account=self.account,
                account_scope=self.account_scope,
                semester=semester,
                limits=self.limits,
                clock=self.clock,
            ).load()
        return self._indices[semester]

    def collect(
        self,
        provider: Any,
        *,
        semester: str,
        course_id: str | None = None,
        include_notice_details: bool = False,
        include_assignment_details: bool = False,
        synthetic: bool = False,
    ) -> dict[str, Any]:
        """Read metadata (and explicitly requested bodies) without downloading."""
        observation = sources.collect(
            provider,
            semester=semester,
            course_id=course_id,
            include_notice_details=include_notice_details,
            include_assignment_details=include_assignment_details,
            limits=self.limits,
            clock=self.clock,
            synthetic=synthetic,
        )
        result = self.ingest(observation)
        result.update(
            adapter_version=observation["adapter_version"],
            counts=observation["counts"],
            read_evidence=observation["read_evidence"],
            downloads=0,
        )
        return result

    def ingest(self, observation: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(observation, dict):
            raise ResourceInvalidArgument("observation")
        semester = validate_semester(observation.get("semester"))
        candidates = observation.get("candidates")
        checked_at = observation.get("checked_at")
        if not isinstance(candidates, list) or any(
            not isinstance(item, ResourceCandidate) for item in candidates
        ):
            raise ResourceInvalidArgument("candidates")
        index = self.index(semester)
        stats = index.merge(
            candidates,
            checked_at=checked_at,
            complete=bool(observation.get("complete")),
            source_errors=observation.get("source_errors") or (),
            warnings=observation.get("warnings") or (),
            mark_absent=bool(observation.get("full_semester") and observation.get("complete")),
            stale_unseen=not bool(observation.get("complete")),
        )
        return {
            "format_version": FACADE_VERSION,
            "semester": semester,
            "checked_at": checked_at,
            "complete": bool(observation.get("complete")),
            "source_errors": clean_codes(observation.get("source_errors")),
            "warnings": clean_codes(observation.get("warnings")),
            "merge": stats,
            "coverage": index.coverage(),
            "downloads": 0,
        }

    # -- reads ---------------------------------------------------------
    def search(
        self,
        *,
        semester: str,
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
        return self.index(semester).search(
            query=query,
            course_id=course_id,
            source=source,
            body_state=body_state,
            updated_after=updated_after,
            updated_before=updated_before,
            state=state,
            include_superseded=include_superseded,
            allow_historical=allow_historical,
            offset=offset,
            limit=limit,
        )

    def coverage(self, semester: str) -> dict[str, Any]:
        return self.index(semester).coverage()

    def manifest(self, semester: str, *, include_superseded: bool = True) -> dict[str, Any]:
        index = self.index(semester)
        entries = [
            entry
            for entry in index.entries
            if include_superseded or entry.state not in ("SUPERSEDED", "MISSING")
        ]
        manifest = build_manifest(entries, index.downloads(), semester=semester)
        manifest["coverage"] = index.coverage()
        return manifest

    def resource(self, semester: str, stable_resource_uid: str, *, version: str | None = None) -> dict[str, Any]:
        index = self.index(semester)
        entry = index.find(stable_resource_uid, version=version)
        records = index.downloads(stable_resource_uid=stable_resource_uid)
        return {
            "format_version": FACADE_VERSION,
            "resource": entry.public(),
            "downloads": [record.public() for record in records],
            "latest_download": records[0].public() if records else None,
        }

    def _select_entries(
        self,
        index: ResourceIndex,
        selections: Iterable[Any],
        *,
        allow_historical: bool = False,
    ) -> list[ResourceEntry]:
        rows: list[ResourceEntry] = []
        seen: set[tuple[str, str]] = set()
        for value in selections:
            uid, version = _selection(value)
            entry = index.find(uid, version=version, allow_historical=True)
            if entry.state == "STALE" and not allow_historical:
                raise ResourceIndexIncomplete("stale_selection")
            key = (entry.stable_resource_uid, entry.version)
            if key in seen:
                continue
            seen.add(key)
            rows.append(entry)
        if not rows:
            raise ResourceInvalidArgument("empty_selection")
        return rows

    # -- download ------------------------------------------------------
    def preview_download(
        self,
        *,
        semester: str,
        selections: Iterable[Any],
        destination: object,
        conflict_policy: str = "REPORT",
        verify_known: bool = False,
        allow_historical: bool = False,
    ) -> dict[str, Any]:
        self._prune_plans()
        index = self.index(semester)
        entries = self._select_entries(index, selections, allow_historical=allow_historical)
        plan = build_plan(
            entries,
            account_scope=self.account_scope,
            semester=index.semester,
            destination=destination,
            conflict_policy=conflict_policy,
            verify_known=verify_known,
            limits=self.limits,
            clock=self.clock,
        )
        self._plans[plan.plan_id] = plan
        return plan.public()

    def _prune_plans(self) -> None:
        now = self.clock()
        kept = {}
        for plan_id, plan in self._plans.items():
            try:
                expires = datetime.fromisoformat(plan.expires_at)
            except ValueError:
                continue
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=now.tzinfo)
            if now < expires:
                kept[plan_id] = plan
        self._plans = kept

    def execute_download(
        self,
        plan_id: str,
        *,
        confirm: bool,
        fetcher: Any = None,
        cancel: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        if confirm is not True:
            from .errors import ResourceConfirmationRequired

            raise ResourceConfirmationRequired("download")
        if not isinstance(plan_id, str) or plan_id not in self._plans:
            raise ResourcePlanExpired("unknown_plan")
        plan = self._plans.pop(plan_id)
        index = self.index(plan.semester)
        return execute_plan(
            plan,
            confirm=confirm,
            index=index,
            fetcher=fetcher if fetcher is not None else self.fetcher,
            limits=self.limits,
            clock=self.clock,
            media=self.media_assets,
            cancel=cancel,
        )

    def download_records(
        self, semester: str, *, stable_resource_uid: str | None = None
    ) -> dict[str, Any]:
        index = self.index(semester)
        records = index.downloads(stable_resource_uid=stable_resource_uid)
        return {
            "format_version": FACADE_VERSION,
            "semester": semester,
            "count": len(records),
            "records": [record.public() for record in records],
        }

    # -- export --------------------------------------------------------
    def export_summary(
        self,
        semester: str,
        *,
        destination: object,
        selections: Iterable[Any] | None = None,
        fields: Iterable[str] | None = None,
        include_body: bool = False,
        acknowledge_personal: bool = False,
    ) -> dict[str, Any]:
        index = self.index(semester)
        if selections is None:
            entries = [
                entry
                for entry in index.entries
                if entry.state == "ACTIVE" and entry.semester == semester
            ]
        else:
            entries = self._select_entries(index, selections)
        result = _export_summary(
            entries,
            destination=destination,
            fields=fields,
            include_body=include_body,
            acknowledge_personal=acknowledge_personal,
            records=index.downloads(),
            limits=self.limits,
            clock=self.clock,
        )
        return result

    # -- lifecycle -----------------------------------------------------
    def delete_cache(self, semester: str) -> dict[str, Any]:
        semester = validate_semester(semester)
        index = self.index(semester)
        cleared_plans = sum(1 for plan in self._plans.values() if plan.semester == semester)
        self._plans = {
            plan_id: plan for plan_id, plan in self._plans.items() if plan.semester != semester
        }
        index.delete()
        self._indices.pop(semester, None)
        return {
            "format_version": FACADE_VERSION,
            "semester": semester,
            "deleted": True,
            "plans_cleared": cleared_plans,
            "checked_at": now_iso(self.clock),
        }

    @staticmethod
    def public_error(error: BaseException) -> dict[str, Any]:
        if isinstance(error, ResourceError):
            return error.as_error()
        if isinstance(error, ResourceStoreUnavailable):
            return error.as_error()
        return {
            "code": "RESOURCE_ERROR",
            "category": "INTERNAL",
            "message": "RESOURCE_ERROR:unknown",
            "retryable": False,
            "automatic_replay_allowed": False,
            "user_action": "CONTACT_SUPPORT",
        }

    def archive_for(self, destination: object) -> Archive:
        return Archive(destination, limits=self.limits)


__all__ = ["FACADE_VERSION", "ResourceCenter"]
