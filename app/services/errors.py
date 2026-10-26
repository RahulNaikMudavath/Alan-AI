"""Domain errors shared by the memory and conflict services.

Kept in a neutral module so ``memory_service``, ``supersession`` and
``conflict_service`` can use them without importing each other. The HTTP layer
maps each to a status code.
"""


class MemoryNotFoundError(LookupError):
    """The memory does not exist for this user."""


class MemorySupersededError(RuntimeError):
    """The memory exists but is no longer active, so it cannot be edited."""


class SupersedeConflictError(RuntimeError):
    """Supersession was requested from an invalid state."""


class ConflictNotFoundError(LookupError):
    """The conflict does not exist for this user."""


class ConflictStateError(RuntimeError):
    """The conflict is already resolved or one of its memories is not active."""
