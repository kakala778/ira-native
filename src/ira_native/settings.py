"""Local-only configuration shared by both interfaces."""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ira_native.errors import IraError


@dataclass(frozen=True)
class Settings:
    database: Path
    allowed_roots: tuple[Path, ...] = ()
    max_pdf_bytes: int = 64 * 1024 * 1024
    max_json_bytes: int = 16 * 1024 * 1024
    max_pages: int = 2000
    max_blocks: int = 50000
    max_search_chars: int = 256
    search_backend: Literal["auto", "like"] = "auto"
    enable_uploads: bool = False

    def __post_init__(self) -> None:
        if min(
            self.max_pdf_bytes,
            self.max_json_bytes,
            self.max_pages,
            self.max_blocks,
            self.max_search_chars,
        ) <= 0 or self.search_backend not in ("auto", "like"):
            raise IraError("invalid_input")

    @classmethod
    def from_env(cls) -> "Settings":
        try:
            return cls(
                database=Path(os.environ.get("IRA_DB", "data/ira.sqlite")),
                allowed_roots=tuple(
                    Path(root)
                    for root in os.environ.get("IRA_ALLOWED_ROOTS", "").split(os.pathsep)
                    if root
                ),
                max_pdf_bytes=int(os.environ.get("IRA_MAX_PDF_BYTES", 64 * 1024 * 1024)),
                max_json_bytes=int(os.environ.get("IRA_MAX_JSON_BYTES", 16 * 1024 * 1024)),
                max_pages=int(os.environ.get("IRA_MAX_PAGES", 2000)),
                max_blocks=int(os.environ.get("IRA_MAX_BLOCKS", 50000)),
                search_backend=os.environ.get("IRA_SEARCH_BACKEND", "auto"),
                enable_uploads=os.environ.get("IRA_ENABLE_UPLOADS") == "1",
            )
        except ValueError:
            raise IraError("invalid_input") from None
