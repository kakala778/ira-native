"""Safe, shared error envelopes for local CLI and HTTP entry points."""

ERRORS: dict[str, tuple[int, str]] = {
    "invalid_input": (422, "The input is invalid."),
    "invalid_location": (422, "The physical page or location is invalid."),
    "invalid_pdf": (422, "The PDF could not be opened or a page could not be read."),
    "parse_failed": (422, "The parsing artifact is not valid for this import."),
    "unsupported_format": (415, "This parsing artifact or media type is unsupported."),
    "source_mismatch": (
        409,
        "The original bytes or page structure disagree with the source claim.",
    ),
    "permission_denied": (403, "Local access to this resource is not authorized."),
    "source_missing": (404, "The original file or registered asset could not be found."),
    "field_verification_missing": (
        409,
        "The historical run lacks an auditable field verification.",
    ),
    "verification_stale": (
        409,
        "The verification inputs or manual records changed. Open a fresh review.",
    ),
    "selection_changed": (409, "配对选择已有更新。请重新打开当前草案或核验结果后再操作。"),
    "pair_not_selected": (409, "该配对未纳入当前核验。请返回草案选择对应清单行，再运行核验。"),
    "source_unreadable": (422, "The source could not be read."),
    "parse_run_not_found": (404, "The registered parse run could not be found."),
    "no_evidence_found": (404, "No matching evidence was found in the searched scope."),
    "resource_limit": (413, "The source exceeds a configured size or complexity limit."),
    "registry_conflict": (409, "This identity is already registered with different metadata."),
    "storage_error": (503, "The local registry is unavailable."),
    "fts_unavailable": (503, "This database requires SQLite FTS5 with the trigram tokenizer."),
    "not_found": (404, "The requested endpoint does not exist."),
    "method_not_allowed": (405, "The HTTP method is not available for this endpoint."),
}


class IraError(Exception):
    def __init__(self, code: str):
        self.code = code
        self.http_status, self.detail = ERRORS[code]
        super().__init__(f"{code}: {self.detail}")

    def payload(self) -> dict[str, str]:
        return {"code": self.code, "detail": self.detail}
