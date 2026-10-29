"""Exceptions shared across modules (kept separate to avoid circular imports)."""


class PbiError(RuntimeError):
    pass


class AuthError(PbiError):
    pass
