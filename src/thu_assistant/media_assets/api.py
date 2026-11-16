"""Public facade for local media preparation.

A caller states what a file is for and who it belongs to. The facade checks the
file boundary, decides the type from content, sanitises images, writes the
served bytes into the controlled temporary area and returns a scoped reference.
It never performs a network request, never returns an absolute path in a public
descriptor and never hands image bytes to a transport unless a single-use grant
for the current request was issued first.
"""
from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from . import images, sniffing
from .boundary import ensure_within, open_verified, sanitize_display_name
from .errors import (
    MediaCancelled,
    MediaError,
    MediaInvalidArgument,
    MediaProcessingTimeout,
    MediaReferenceCorrupt,
    MediaStateUnavailable,
)
from .limits import DEFAULT_LIMITS, MediaLimits
from .model_input import (
    ModelAssetBundle,
    ModelInputBroker,
    TransferGrant,
    capability_report,
)
from .registry import (
    ACCOUNT_DOMAINS,
    KINDS,
    PURPOSES,
    RETENTIONS,
    SERVICE_DOMAINS,
    SOURCE_KINDS,
    MediaReference,
    MediaStateStore,
    ReferenceRegistry,
    expiry_for,
    utc_now,
    validate_choice,
    validate_scope,
    validate_task,
    verify_bytes,
)
from .tempstore import CleanupReport, TempArea

_SUFFIX_BY_MIME = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "application/pdf": ".pdf",
    "application/zip": ".zip",
    "application/gzip": ".gz",
    "application/json": ".json",
    "text/csv": ".csv",
    "text/markdown": ".md",
    "text/plain": ".txt",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
}


@dataclass
class MediaAudit:
    """Counters used by tests and diagnostics; no hidden global cache."""

    source_reads: int = 0
    network_attempts: int = 0

    def as_dict(self, *, temp: TempArea, references: int) -> dict[str, int]:
        return {
            "source_reads": self.source_reads,
            "temp_writes": temp.written,
            "temp_deletes": temp.deleted,
            "network_attempts": self.network_attempts,
            "references": references,
        }


class MediaAssets:
    """The only supported entry point for local image and attachment handling."""

    def __init__(
        self,
        temp_root: str | os.PathLike[str],
        *,
        limits: MediaLimits | None = None,
        clock: Callable[[], datetime] = utc_now,
        state_store: Any = None,
        write_hook: Callable[[int, bytes], int] | None = None,
    ) -> None:
        self.limits = limits or DEFAULT_LIMITS
        self.clock = clock
        self.temp = TempArea(temp_root, write_hook=write_hook)
        self.registry = ReferenceRegistry()
        self.temp.keep_provider = self.registry.retained_paths
        self.broker = ModelInputBroker(self.registry, self.limits, clock=clock)
        self.audit = MediaAudit()
        self.state_store = state_store

    # -- registration -------------------------------------------------
    def register_user_file(self, path: object, **options: Any) -> MediaReference:
        return self._register(path, source_kind="USER_SELECTED", **options)

    def register_downloaded_attachment(self, path: object, **options: Any) -> MediaReference:
        return self._register(path, source_kind="DOWNLOADED_ATTACHMENT", **options)

    def register_synthetic_file(self, path: object, **options: Any) -> MediaReference:
        options.setdefault("synthetic", True)
        return self._register(path, source_kind="SYNTHETIC", **options)

    def _register(
        self,
        path: object,
        *,
        source_kind: str,
        kind: str = "IMAGE",
        account_scope: str,
        account_domain: str = "CAMPUS",
        service_domain: str = "LOCAL",
        task_id: str,
        purpose: str = "USER_REVIEW",
        source_evidence_id: str = "local.user_selected",
        declared_mime: str | None = None,
        display_name: str | None = None,
        external_transfer_allowed: bool = False,
        retention: str | None = None,
        synthetic: bool = False,
        expires_in: int | None = None,
        cancel: Callable[[], bool] | None = None,
        deadline: float | None = None,
    ) -> MediaReference:
        validate_choice(source_kind, name="source_kind", choices=SOURCE_KINDS)
        validate_choice(kind, name="kind", choices=KINDS)
        validate_choice(purpose, name="purpose", choices=PURPOSES)
        validate_choice(account_domain, name="account_domain", choices=ACCOUNT_DOMAINS)
        validate_choice(service_domain, name="service_domain", choices=SERVICE_DOMAINS)
        retention = retention or (
            "SESSION" if purpose == "MODEL_INPUT" else "EXPLICIT_DELETE"
        )
        validate_choice(retention, name="retention", choices=RETENTIONS)
        validate_scope(account_scope, name="account_scope")
        validate_task(task_id)
        if not isinstance(source_evidence_id, str) or not (
            1 <= len(source_evidence_id) <= 120
        ):
            raise MediaInvalidArgument("source_evidence_id")
        if not isinstance(external_transfer_allowed, bool):
            raise MediaInvalidArgument("external_transfer_allowed")
        if not isinstance(synthetic, bool):
            raise MediaInvalidArgument("synthetic")
        if declared_mime is not None and not isinstance(declared_mime, str):
            raise MediaInvalidArgument("declared_mime")

        gate = self._gate(cancel, deadline)
        moment = self.clock()
        expires_at = (
            expiry_for(moment, self.limits, seconds=expires_in)
            if retention != "PROJECT_CACHE"
            else None
        )
        max_bytes = (
            self.limits.max_image_bytes
            if kind == "IMAGE"
            else self.limits.max_attachment_bytes
        )

        with open_verified(path, max_bytes=max_bytes, display_name=display_name) as handle:
            source_name = handle.path.name
            mime_type = sniffing.verify(
                name=source_name,
                head=handle.head,
                declared_mime=declared_mime,
                kind=kind,
                limits=self.limits,
            )
            gate()

            if kind == "IMAGE":
                original = handle.read_all()
                self.audit.source_reads += 1
                source_sha256 = hashlib.sha256(original).hexdigest()
                gate()
                result = images.sanitize(
                    original, mime_type=mime_type, limits=self.limits, cancel=gate
                )
                gate()
                stored = self.temp.write_bytes(
                    result.data, suffix=_SUFFIX_BY_MIME[result.mime_type]
                )
                byte_size = result.byte_size
                sha256 = stored.sha256
                width: int | None = result.width
                height: int | None = result.height
                pixel_count: int | None = result.pixel_count
                transform = result.transform_note()
                served_mime = result.mime_type
            else:
                hasher = hashlib.sha256()

                def chunks():
                    checkpoint = 8 * 1024 * 1024
                    seen = 0
                    for chunk in handle.iter_chunks():
                        hasher.update(chunk)
                        seen += len(chunk)
                        if seen >= checkpoint:
                            checkpoint += 8 * 1024 * 1024
                            gate()
                        yield chunk

                stored = self.temp.write_stream(
                    chunks(), suffix=_SUFFIX_BY_MIME.get(mime_type, ".bin")
                )
                self.audit.source_reads += 1
                source_sha256 = hasher.hexdigest()
                byte_size = stored.byte_size
                sha256 = stored.sha256
                width = height = pixel_count = None
                transform = {
                    "operations": ["stored_verbatim"],
                    "orientation_applied": False,
                    "metadata_removed": False,
                    "reencoded_as": None,
                }
                served_mime = mime_type

        gate()
        reference = MediaReference(
            media_id="media_" + os.urandom(12).hex(),
            kind=kind,
            mime_type=served_mime,
            byte_size=byte_size,
            sha256=sha256,
            width=width,
            height=height,
            pixel_count=pixel_count,
            source_evidence_id=source_evidence_id,
            account_domain=account_domain,
            service_domain=service_domain,
            account_scope=account_scope,
            task_id=task_id,
            purpose=purpose,
            source_kind=source_kind,
            created_at=moment,
            expires_at=expires_at,
            retention=retention,
            display_name=sanitize_display_name(
                display_name if display_name is not None else Path(source_name).name
            ),
            synthetic=synthetic,
            external_transfer_allowed=external_transfer_allowed,
            path=stored.path,
            source_sha256=source_sha256,
            source_mime=mime_type,
            transform=transform,
        )
        if retention == "PROJECT_CACHE":
            self.temp.mark_persistent(stored.path)
        try:
            self.registry.add(reference)
        except BaseException:
            self.temp.discard(stored.path)
            raise
        try:
            self.save_state(account_scope)
        except BaseException:
            self.registry.remove(reference.media_id)
            self.temp.discard(stored.path)
            raise
        return reference

    @staticmethod
    def _gate(
        cancel: Callable[[], bool] | None, deadline: float | None
    ) -> Callable[[], None]:
        """One cancellation/expiry gate shared by every processing stage."""

        def check() -> None:
            if deadline is not None and time.monotonic() >= deadline:
                raise MediaProcessingTimeout("elapsed")
            if cancel is not None and cancel():
                raise MediaCancelled("caller")

        return check

    @staticmethod
    def _check_cancel(cancel: Callable[[], bool] | None) -> None:
        if cancel is not None and cancel():
            raise MediaCancelled("caller")

    # -- reading ------------------------------------------------------
    def resolve(
        self,
        media_id: object,
        *,
        account_scope: str,
        task_id: str,
        purpose: str | None = None,
    ) -> MediaReference:
        validate_scope(account_scope, name="account_scope")
        validate_task(task_id)
        if purpose is not None:
            validate_choice(purpose, name="purpose", choices=PURPOSES)
        return self.registry.resolve(
            media_id,
            account_scope=account_scope,
            task_id=task_id,
            purpose=purpose,
            now=self.clock(),
        )

    def descriptor(
        self,
        media_id: object,
        *,
        account_scope: str,
        task_id: str,
        purpose: str | None = None,
    ) -> dict[str, Any]:
        reference = self.resolve(
            media_id, account_scope=account_scope, task_id=task_id, purpose=purpose
        )
        return reference.to_reference()

    def source_info(
        self,
        media_id: object,
        *,
        account_scope: str,
        task_id: str,
        purpose: str | None = None,
    ) -> dict[str, Any]:
        """Source fields the resource/course module needs; no local path."""
        reference = self.resolve(
            media_id, account_scope=account_scope, task_id=task_id, purpose=purpose
        )
        entry = reference.to_manifest_entry()
        return {
            "asset_id": entry["asset_id"],
            "kind": entry["kind"],
            "mime_type": entry["mime_type"],
            "byte_size": entry["byte_size"],
            "sha256": entry["sha256"],
            "source": entry["source"],
            "purpose": entry["purpose"],
            "expires_at": entry["expires_at"],
        }

    def manifest(
        self,
        media_ids: Iterable[object],
        *,
        account_scope: str,
        task_id: str,
        purpose: str | None = None,
    ) -> dict[str, Any]:
        validate_scope(account_scope, name="account_scope")
        validate_task(task_id)
        if isinstance(media_ids, (str, bytes)):
            raise MediaInvalidArgument("media_ids_not_list")
        entries = []
        total = 0
        for media_id in media_ids:
            reference = self.resolve(
                media_id, account_scope=account_scope, task_id=task_id, purpose=purpose
            )
            entry = reference.to_manifest_entry()
            entries.append(entry)
            total += int(entry["byte_size"])
        return {
            "account_scope": account_scope,
            "task_id": task_id,
            "count": len(entries),
            "total_bytes": total,
            "assets": entries,
        }

    def read_bytes(
        self,
        media_id: object,
        *,
        account_scope: str,
        task_id: str,
        purpose: str | None = None,
        cancel: Callable[[], bool] | None = None,
    ) -> bytes:
        reference = self.resolve(
            media_id, account_scope=account_scope, task_id=task_id, purpose=purpose
        )
        return self._read_reference(reference, cancel=cancel)

    def _read_reference(
        self, reference: MediaReference, *, cancel: Callable[[], bool] | None = None
    ) -> bytes:
        limit = (
            self.limits.max_image_bytes
            if reference.kind == "IMAGE"
            else self.limits.max_attachment_bytes
        )
        try:
            ensure_within(self.temp.root, reference.path)
        except Exception:
            raise MediaReferenceCorrupt("outside_root") from None
        try:
            with open_verified(reference.path, max_bytes=limit) as handle:
                data = handle.read_all()
        except MediaError as error:
            detail = "size" if error.code == "MEDIA_SIZE_LIMIT" else "unreadable"
            raise MediaReferenceCorrupt(detail) from None
        except Exception:
            raise MediaReferenceCorrupt("read_failed") from None
        self.audit.source_reads += 1
        self._check_cancel(cancel)
        verify_bytes(reference, data)
        return data

    # -- model outbound -----------------------------------------------
    def prepare_model_bundle(
        self,
        media_ids: Iterable[object],
        *,
        account_scope: str,
        task_id: str,
        request_id: str,
        model_id: str,
        prompt_text: str | None = None,
    ) -> ModelAssetBundle:
        validate_scope(account_scope, name="account_scope")
        validate_task(task_id)
        return self.broker.prepare(
            media_ids,
            account_scope=account_scope,
            task_id=task_id,
            request_id=request_id,
            model_id=model_id,
            prompt_text=prompt_text,
            now=self.clock(),
        )

    def authorize_model_send(
        self, bundle_id: object, *, request_id: str
    ) -> TransferGrant:
        return self.broker.authorize(bundle_id, request_id=request_id, now=self.clock())

    def consume_model_bundle(
        self, grant: TransferGrant, *, request_id: str
    ) -> tuple[Any, ...]:
        return self.broker.consume(
            grant,
            request_id=request_id,
            reader=self._read_reference,
            now=self.clock(),
        )

    def capability(self, **kwargs: Any) -> dict[str, Any]:
        """Capability probe: static evidence only, no reference resolution."""
        return capability_report(**kwargs)

    # -- lifecycle ----------------------------------------------------
    def cancel_task(self, task_id: str) -> dict[str, Any]:
        validate_task(task_id)
        references = self.registry.revoke_task(task_id)
        removed = 0
        for reference in references:
            if self.temp.contains(reference.path) and self.temp.discard(reference.path):
                removed += 1
        for bundle_id in self.broker.active():
            bundle = self.broker.get(bundle_id)
            if bundle.task_id == task_id:
                self.broker.cancel(bundle_id)
        if references:
            self.save_state(references[0].account_scope)
        return {"task_id": task_id, "revoked": len(references), "removed": removed}

    def cleanup(self, *, min_age_seconds: float | None = None) -> CleanupReport:
        grace = (
            self.limits.cleanup_grace_seconds
            if min_age_seconds is None
            else min_age_seconds
        )
        return self.temp.sweep(
            keep=self.registry.active_paths(), min_age_seconds=grace
        )

    def close(self) -> CleanupReport:
        """Process-exit cleanup: SESSION files go, explicit retentions stay."""
        for bundle_id in list(self.broker.active()):
            self.broker.cancel(bundle_id)
        return self.temp.close()

    def audit_report(self) -> dict[str, int]:
        return self.audit.as_dict(temp=self.temp, references=len(self.registry))

    # -- optional encrypted state -------------------------------------
    def _state_for(self, account_scope: str) -> MediaStateStore:
        return MediaStateStore(
            self.state_store,
            record_id=f"media-{account_scope}",
            account_domain="CAMPUS",
            service_domain="LOCAL",
        )

    def save_state(self, account_scope: str) -> None:
        if self.state_store is None:
            return
        entries = [
            entry
            for entry in self.registry.state_entries()
            if entry.get("account_scope") == account_scope
        ]
        self._state_for(account_scope).save(entries, account=account_scope)

    def restore_state(self, account_scope: str) -> int:
        if self.state_store is None:
            raise MediaStateUnavailable("state_store_absent")
        validate_scope(account_scope, name="account_scope")
        entries = self._state_for(account_scope).load(account=account_scope)
        restored = 0
        for entry in entries:
            try:
                reference = MediaReference.from_state_entry(entry, root=self.temp.root)
            except (KeyError, TypeError, ValueError):
                continue
            if reference.account_scope != account_scope:
                continue
            if self.registry.restore(reference):
                restored += 1
        return restored


def build(temp_root: str | os.PathLike[str], **options: Any) -> MediaAssets:
    return MediaAssets(temp_root, **options)


__all__ = ["MediaAssets", "MediaAudit", "build"]
