"""Fixture-only, no-action EEG transport smoke adapter."""

__all__ = ["LoopError", "fixture_features", "run_fixture_loop"]


def __getattr__(name):
    if name in __all__:
        from . import loop
        return getattr(loop, name)
    raise AttributeError(name)
