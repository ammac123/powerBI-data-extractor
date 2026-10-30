"""Per-invocation state shared by commands: config, settings and the browser session."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

from pydantic import ValidationError

from .errors import PbiError
from .session import PbiSession
from .settings import RunConfig, Settings


@dataclass
class Context:
    config_path: Path
    headed: bool = False

    @cached_property
    def cfg(self) -> RunConfig:
        return RunConfig.load(self.config_path)

    @cached_property
    def settings(self) -> Settings:
        try:
            return Settings()  # type: ignore[call-arg]  # populated from env / .env
        except ValidationError as e:
            names = ", ".join(sorted({f"PBI_{err['loc'][0]}".upper() for err in e.errors()}))
            raise PbiError(f"missing or invalid settings: {names}. Set them in the environment or a .env file") from e

    @contextmanager
    def session(self) -> Iterator[PbiSession]:
        with PbiSession(self.settings, self.cfg, headed=self.headed).open() as s:
            yield s
