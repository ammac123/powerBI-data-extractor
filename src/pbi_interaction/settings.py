"""Secrets and environment config (env vars > .env > defaults) plus the YAML run config."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import keyring
import yaml
from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

KEYRING_SERVICE = "pbi-interaction"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PBI_", env_file=".env", extra="ignore")

    site_url: str
    username: str
    password: SecretStr | None = None
    headless: bool = True
    state_path: Path = Path(".state/storage_state.json")
    timeout_s: float = 60.0
    out_dir: Path = Path("out")
    debug_dir: Path = Path("debug")

    def resolved_password(self) -> SecretStr:
        """PBI_PASSWORD wins; otherwise fall back to the macOS Keychain entry for this username."""
        if self.password is not None:
            return self.password
        stored = keyring.get_password(KEYRING_SERVICE, self.username)
        if not stored:
            raise RuntimeError(
                "No password: set PBI_PASSWORD, or store one with "
                f"`keyring set {KEYRING_SERVICE} {self.username}`"
            )
        return SecretStr(stored)


class LoginSelectors(BaseModel):
    username: str = "input[name='username']"
    password: str = "input[name='password']"
    submit: str = "button[type='submit']"


class FilterSpec(BaseModel):
    """Either a basic 'In' filter (table/column/values) or a raw SDK filter object."""

    level: str = "report"  # report | page | visual
    page: str | None = None
    visual: str | None = None
    table: str | None = None
    column: str | None = None
    values: list[Any] = Field(default_factory=list)
    raw: dict[str, Any] | None = None

    def to_js(self) -> dict[str, Any]:
        if self.raw is not None:
            sdk_filter = self.raw
        else:
            sdk_filter = {
                "$schema": "http://powerbi.com/product/schema#basic",
                "target": {"table": self.table, "column": self.column},
                "operator": "In",
                "values": self.values,
                "filterType": 1,
            }
        return {"level": self.level, "page": self.page, "visual": self.visual, "filter": sdk_filter}


class RunNode(BaseModel):
    """A node in the run tree. Children inherit page/visual/export_type/filters from their parent.

    A node with no children (and no matrix) is a leaf and produces exactly one export.
    `matrix` is shorthand for children covering the cartesian product of the axes' values.
    """

    label: str
    page: str | None = None
    visual: str | None = None
    export_type: str | None = None
    filters: list[FilterSpec] = Field(default_factory=list)
    matrix: list[FilterSpec] = Field(default_factory=list)
    children: list["RunNode"] = Field(default_factory=list)


class RunConfig(BaseModel):
    embed_wrapper: str = "#power-bi-iframe-wrapper"
    login: LoginSelectors = LoginSelectors()
    page: str | None = None
    visual: str | None = None
    keep_visuals: list[str] = Field(default_factory=list)
    hide_others: bool = True
    filters: list[FilterSpec] = Field(default_factory=list)
    runs: list[RunNode] = Field(default_factory=list)  # empty -> a single run from the fields above
    export_type: str = "Summarized"  # Summarized | Underlying
    settle_ms: int = 750
    stable_reads: int = 2

    @classmethod
    def load(cls, path: Path) -> "RunConfig":
        if not path.exists():
            return cls()
        return cls.model_validate(yaml.safe_load(path.read_text()) or {})
