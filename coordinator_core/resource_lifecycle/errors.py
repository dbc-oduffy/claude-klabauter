"""Shared error types for the resource-lifecycle primitives."""

from __future__ import annotations


class AllowanceExceeded(Exception):
    """A write would exceed a store's allowance; nothing was evicted.

    `remediation` tells the caller what to do; str() renders it.
    """

    def __init__(self, message: str, remediation: str) -> None:
        super().__init__(message)
        self.message = message
        self.remediation: str = remediation

    def __str__(self) -> str:
        return f"{self.message} Remediation: {self.remediation}"


class LeaseHeld(Exception):
    """The lease is already held by another live owner."""
