"""Result dataclasses used by sync/provision style operations."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SyncSettingsResult:
    copied: list[str] = field(default_factory=list)
    skipped_missing: list[str] = field(default_factory=list)
    skipped_exists: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, list[str]]:
        return {
            'copied': list(self.copied),
            'skipped_missing': list(self.skipped_missing),
            'skipped_exists': list(self.skipped_exists),
            'failed': list(self.failed),
        }
