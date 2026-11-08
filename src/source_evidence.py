"""Deterministic evidence envelopes for provider snapshot rows."""

import hashlib
import json
from dataclasses import dataclass
from typing import Any


SOURCE_ROW_EVIDENCE_VERSION = "source-row-evidence-v1"


@dataclass(frozen=True)
class SourceRevisionEvidence:
    snapshot_row_identity: str
    provider_logical_record_id: str | None
    logical_identity_status: str
    ordering_status: str
    supersedes_snapshot_row_identity: str | None
    update_contract: str | None
    ordering_evidence_ref: str | None


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def canonical_content_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _snapshot_row_identity(
    *,
    source: str,
    snapshot_id: str,
    snapshot_sha256: str,
    source_file_path: str | None,
    filename: str | None,
    row_locator: dict[str, Any],
) -> str:
    identity = {
        "schema_version": SOURCE_ROW_EVIDENCE_VERSION,
        "source": source,
        "snapshot_id": snapshot_id,
        "snapshot_sha256": snapshot_sha256,
        "source_file_path": source_file_path,
        "filename": filename,
        "row_locator": row_locator,
    }
    return f"snapshot-row-v1:{canonical_content_sha256(identity)}"


def build_source_row_evidence(
    *,
    source: str,
    legacy_source_id: str,
    snapshot_id: str,
    snapshot_sha256: str,
    source_repository: str | None,
    source_file_path: str | None,
    filename: str | None,
    row_locator: dict[str, Any],
    raw_row_content: Any,
    availability: dict[str, Any],
    logical_identity_unavailable_reason: str,
    adapter_enrichment: dict[str, Any] | None = None,
) -> dict[str, Any]:
    row_content_sha256 = canonical_content_sha256(raw_row_content)
    snapshot_row_id = _snapshot_row_identity(
        source=source,
        snapshot_id=snapshot_id,
        snapshot_sha256=snapshot_sha256,
        source_file_path=source_file_path,
        filename=filename,
        row_locator=row_locator,
    )
    return {
        "schema_version": SOURCE_ROW_EVIDENCE_VERSION,
        "source": source,
        "legacy_source_id": legacy_source_id,
        "snapshot": {
            "snapshot_id": snapshot_id,
            "snapshot_sha256": snapshot_sha256,
            "source_repository": source_repository,
            "source_file_path": source_file_path,
            "filename": filename,
        },
        "row": {
            "locator": row_locator,
            "raw_content": raw_row_content,
            "content_sha256": row_content_sha256,
        },
        "identity": {
            "snapshot_scoped_row_id": snapshot_row_id,
            "provider_logical_record_id": None,
            "provider_logical_record_status": "unavailable",
            "provider_logical_record_reason": logical_identity_unavailable_reason,
        },
        "revision": {
            "value": None,
            "evidence_basis": "not_supplied_by_adapter",
            "ordering_status": "unavailable",
        },
        "availability": availability,
        "adapter_enrichment": adapter_enrichment or {},
    }


def persisted_snapshot_row_id(
    raw_reference: str | None,
    *,
    source: str,
    legacy_source_id: str,
    snapshot_id: str | None,
    snapshot_sha256: str | None,
) -> str:
    """Return a validated snapshot-scoped storage key, or a legacy source ID."""
    if not raw_reference:
        return legacy_source_id
    try:
        reference = json.loads(raw_reference)
    except (TypeError, ValueError):
        return legacy_source_id
    if not isinstance(reference, dict):
        return legacy_source_id
    evidence = reference.get("source_row_evidence")
    if evidence is None:
        return legacy_source_id
    if not isinstance(evidence, dict) or evidence.get("schema_version") != SOURCE_ROW_EVIDENCE_VERSION:
        raise ValueError("unsupported source_row_evidence schema")

    expected_id = _validated_snapshot_row_identity(
        evidence,
        source=source,
        legacy_source_id=legacy_source_id,
        snapshot_id=snapshot_id,
        snapshot_sha256=snapshot_sha256,
    )
    _validated_logical_identity(evidence["identity"])
    return expected_id


def read_source_revision_evidence(
    raw_reference: str | dict[str, Any] | None,
    *,
    source: str,
    legacy_source_id: str,
    snapshot_id: str | None,
    snapshot_sha256: str | None,
    source_revision: int,
) -> SourceRevisionEvidence | None:
    """Read explicit revision links without deriving them from numeric revisions."""
    if isinstance(raw_reference, dict):
        reference = raw_reference
    elif isinstance(raw_reference, str) and raw_reference:
        try:
            reference = json.loads(raw_reference)
        except (TypeError, ValueError):
            return None
    else:
        return None
    if not isinstance(reference, dict):
        return None
    evidence = reference.get("source_row_evidence")
    if evidence is None:
        return None
    if not isinstance(evidence, dict) or evidence.get("schema_version") != SOURCE_ROW_EVIDENCE_VERSION:
        raise ValueError("unsupported source_row_evidence schema")

    snapshot_row_identity = _validated_snapshot_row_identity(
        evidence,
        source=source,
        legacy_source_id=legacy_source_id,
        snapshot_id=snapshot_id,
        snapshot_sha256=snapshot_sha256,
    )
    identity = evidence["identity"]
    logical_record_id, identity_status = _validated_logical_identity(identity)
    revision = evidence.get("revision")
    if not isinstance(revision, dict):
        raise ValueError("source_row_evidence revision must be an object")

    ordering_status = revision.get("ordering_status")
    if ordering_status not in {"unavailable", "verified"}:
        raise ValueError("source_row_evidence revision ordering_status is invalid")
    if ordering_status == "unavailable":
        if revision.get("supersedes_snapshot_row_identity") is not None:
            raise ValueError("unverified revision ordering cannot declare a superseding row")
        return SourceRevisionEvidence(
            snapshot_row_identity=snapshot_row_identity,
            provider_logical_record_id=logical_record_id,
            logical_identity_status=identity_status,
            ordering_status=ordering_status,
            supersedes_snapshot_row_identity=None,
            update_contract=None,
            ordering_evidence_ref=None,
        )

    if identity_status != "verified" or logical_record_id is None:
        raise ValueError("verified revision ordering requires a verified provider logical identity")
    if revision.get("value") != source_revision:
        raise ValueError("source_row_evidence revision value does not match source_revision")
    if not isinstance(revision.get("evidence_basis"), str) or not revision["evidence_basis"].strip():
        raise ValueError("verified revision ordering requires an evidence_basis")
    evidence_ref = revision.get("ordering_evidence_ref")
    if not isinstance(evidence_ref, str) or not evidence_ref.strip():
        raise ValueError("verified revision ordering requires an ordering_evidence_ref")
    update_contract = revision.get("update_contract")
    if update_contract != "complete_replacement":
        raise ValueError("verified revision ordering requires a complete_replacement update_contract")
    parent_identity = revision.get("supersedes_snapshot_row_identity")
    if parent_identity is not None and (
        not isinstance(parent_identity, str) or not parent_identity.strip()
    ):
        raise ValueError("supersedes_snapshot_row_identity must be a non-empty string or None")

    return SourceRevisionEvidence(
        snapshot_row_identity=snapshot_row_identity,
        provider_logical_record_id=logical_record_id,
        logical_identity_status=identity_status,
        ordering_status=ordering_status,
        supersedes_snapshot_row_identity=parent_identity,
        update_contract=update_contract,
        ordering_evidence_ref=evidence_ref,
    )


def _validated_snapshot_row_identity(
    evidence: dict[str, Any],
    *,
    source: str,
    legacy_source_id: str,
    snapshot_id: str | None,
    snapshot_sha256: str | None,
) -> str:
    snapshot = evidence.get("snapshot")
    row = evidence.get("row")
    identity = evidence.get("identity")
    if not all(isinstance(value, dict) for value in (snapshot, row, identity)):
        raise ValueError("source_row_evidence is missing snapshot, row, or identity metadata")
    if evidence.get("source") != source or evidence.get("legacy_source_id") != legacy_source_id:
        raise ValueError("source_row_evidence does not match the source row identity")
    if snapshot.get("snapshot_id") != snapshot_id or snapshot.get("snapshot_sha256") != snapshot_sha256:
        raise ValueError("source_row_evidence snapshot metadata does not match the source row")

    locator = row.get("locator")
    raw_content = row.get("raw_content")
    if not isinstance(locator, dict):
        raise ValueError("source_row_evidence row locator must be an object")
    if row.get("content_sha256") != canonical_content_sha256(raw_content):
        raise ValueError("source_row_evidence row content hash mismatch")

    expected_id = _snapshot_row_identity(
        source=source,
        snapshot_id=snapshot_id,
        snapshot_sha256=snapshot_sha256,
        source_file_path=snapshot.get("source_file_path"),
        filename=snapshot.get("filename"),
        row_locator=locator,
    )
    if identity.get("snapshot_scoped_row_id") != expected_id:
        raise ValueError("source_row_evidence snapshot-scoped identity mismatch")
    return expected_id


def _validated_logical_identity(identity: dict[str, Any]) -> tuple[str | None, str]:
    status = identity.get("provider_logical_record_status", "unavailable")
    logical_record_id = identity.get("provider_logical_record_id")
    if status == "unavailable":
        if logical_record_id is not None:
            raise ValueError("unavailable provider logical identity must be null")
        return None, status
    if status != "verified" or not isinstance(logical_record_id, str) or not logical_record_id.strip():
        raise ValueError("provider logical identity must be explicitly verified or unavailable")
    proof = identity.get("provider_logical_record_evidence")
    if not isinstance(proof, dict):
        raise ValueError("verified provider logical identity requires evidence")
    if any(not isinstance(proof.get(key), str) or not proof[key].strip() for key in ("basis", "reference")):
        raise ValueError("verified provider logical identity requires basis and reference evidence")
    return logical_record_id, status


def legacy_source_id_from_reference(raw_reference: str | None) -> str | None:
    if not raw_reference:
        return None
    try:
        reference = json.loads(raw_reference)
    except (TypeError, ValueError):
        return None
    if not isinstance(reference, dict):
        return None
    evidence = reference.get("source_row_evidence")
    if not isinstance(evidence, dict):
        return None
    legacy_source_id = evidence.get("legacy_source_id")
    return legacy_source_id if isinstance(legacy_source_id, str) else None