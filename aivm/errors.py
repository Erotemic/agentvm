"""Project-specific exception types."""

from __future__ import annotations


class AIVMError(RuntimeError):
    """Base error for domain-level aivm failures."""


class MissingSSHIdentityError(AIVMError):
    """Raised when SSH identity configuration is required but missing."""
