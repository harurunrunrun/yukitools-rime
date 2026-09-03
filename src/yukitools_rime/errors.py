"""Application-level exceptions with safe, user-facing messages."""

from __future__ import annotations


class AppError(Exception):
    """Base class for expected failures reported without a traceback."""

    exit_code = 1


class UsageError(AppError):
    """A semantically invalid combination of command-line arguments."""

    exit_code = 2


class ValidationError(AppError, ValueError):
    """Data failed validation before any local or remote mutation."""


class ConfigError(ValidationError):
    """A Rime/yukicoder configuration file is invalid or ambiguous."""


class LayoutError(ConfigError):
    """The requested path is not part of a supported Rime layout."""


class FileOperationError(AppError, OSError):
    """A local file could not be read or updated safely."""


class AuthenticationError(AppError):
    """No usable credential was found for an authenticated operation."""


class APIError(AppError):
    """The yukicoder API rejected or could not complete an operation."""


class ConflictError(AppError):
    """Local and remote state require an explicit conflict decision."""


class NonInteractiveError(AppError):
    """An operation needed confirmation but no interactive input was available."""


class UserCancelledError(AppError):
    """The user declined an optional destructive replacement."""


__all__ = [
    "APIError",
    "AppError",
    "AuthenticationError",
    "ConfigError",
    "ConflictError",
    "FileOperationError",
    "LayoutError",
    "NonInteractiveError",
    "UsageError",
    "UserCancelledError",
    "ValidationError",
]
