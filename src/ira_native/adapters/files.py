"""Read only explicitly allowed local files and enforce byte limits."""

import os
import stat
from pathlib import Path

from ira_native.errors import IraError


class LocalFiles:
    def __init__(self, allowed_roots: tuple[Path, ...]):
        self.roots = tuple(self._resolve(root) for root in allowed_roots)

    @staticmethod
    def _resolve(path: Path) -> Path:
        try:
            return path.resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            raise IraError("source_unreadable") from None

    def authorize(self, path: Path) -> Path:
        if (
            ".." in path.parts
            or path.is_reserved()
            or path.drive.startswith("\\\\")
            or any(":" in part for part in path.parts if part != path.anchor)
        ):
            raise IraError("permission_denied")
        # Reject out-of-scope paths before following filesystem links.
        lexical = Path(os.path.abspath(path))
        if not any(lexical.is_relative_to(root) for root in self.roots):
            raise IraError("permission_denied")
        resolved = self._resolve(lexical)
        if not any(resolved.is_relative_to(root) for root in self.roots):
            raise IraError("permission_denied")
        return resolved

    def read(self, path: Path, limit: int) -> tuple[Path, bytes]:
        resolved = self.authorize(path)
        try:
            info = resolved.stat()
            if not stat.S_ISREG(info.st_mode):
                raise IraError("permission_denied")
            if info.st_size > limit:
                raise IraError("resource_limit")
            with resolved.open("rb") as file:
                if not os.path.samestat(info, os.fstat(file.fileno())):
                    raise IraError("source_unreadable")
                data = file.read(limit + 1)
        except FileNotFoundError:
            raise IraError("source_missing") from None
        except PermissionError:
            raise IraError("permission_denied") from None
        except (OSError, ValueError):
            raise IraError("source_unreadable") from None
        if len(data) > limit:
            raise IraError("resource_limit")
        return resolved, data
