"""Errors a player can trigger. `message` is safe to show in an ephemeral reply."""

from __future__ import annotations


class DomainError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NotAllowed(DomainError):
    """The person clicking is not allowed to take this action."""


class StaleAction(DomainError):
    """The click refers to a step that has already passed. An expected race, not a fault."""


class InvalidAction(DomainError):
    """The action breaks a rule, such as picking a banned champion."""
