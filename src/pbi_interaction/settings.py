"""Secrets and environment config (env vars > .env > defaults) plus the YAML run config."""

from pathlib import Path
from typing import Any, Literal, Self

import keyring
import yaml
from pydantic import BaseModel, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .errors import AuthError

KEYRING_SERVICE = "pbi-interaction"
BASIC_FILTER_TYPE = 1  # powerbi-models FilterType.Basic

Level = Literal["report", "page", "visual"]
ExportType = Literal["Summarized", "Underlying"]


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
            raise AuthError(
                "No password: set PBI_PASSWORD, or store one with "
                f"`keyring set {KEYRING_SERVICE} {self.username}`"
            )
        return SecretStr(stored)


class LoginSelectors(BaseModel):
    username: str = "input[name='username']"
    password: str = "input[name='password']"
    submit: str = "button[type='submit']"


class FilterSpec(BaseModel):
    """Either a basic 'In' filter (table/column/values) or a raw SDK filter object.

    page/visual may be omitted for page/visual-level filters; they then default to the run's own.
    """

    level: Level = "report"
    page: str | None = None
    visual: str | None = None
    table: str | None = None
    column: str | None = None
    values: list[Any] = Field(default_factory=list)
    raw: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _check_shape(self) -> Self:
        basic = (self.table, self.column) != (None, None) or bool(self.values)
        if self.raw is not None:
            if basic:
                raise ValueError("filter: use either raw or table/column/values, not both")
        elif not (self.table and self.column and self.values):
            raise ValueError("filter: needs raw, or table, column and values")
        if self.level == "report" and (self.page or self.visual):
            raise ValueError("report-level filter cannot set page or visual")
        if self.level == "page" and self.visual:
            raise ValueError("page-level filter cannot set visual")
        return self

    def sdk_filter(self) -> dict[str, Any]:
        if self.raw is not None:
            return self.raw
        return {
            "$schema": "http://powerbi.com/product/schema#basic",
            "target": {"table": self.table, "column": self.column},
            "operator": "In",
            "values": self.values,
            "filterType": BASIC_FILTER_TYPE,
        }


class RunNode(BaseModel):
    """A node in the run tree. Children inherit page/visual/export_type/filters from their parent.

    A node with no children (and no matrix) is a leaf and produces exactly one export.
    `matrix` is shorthand for children covering the cartesian product of the axes' values.
    """

    label: str
    page: str | None = None
    visual: str | None = None
    export_type: ExportType | None = None
    filters: list[FilterSpec] = Field(default_factory=list)
    matrix: list[FilterSpec] = Field(default_factory=list)
    children: list[RunNode] = Field(default_factory=list)


class RunConfig(BaseModel):
    embed_wrapper: str = "#power-bi-iframe-wrapper"
    login: LoginSelectors = LoginSelectors()
    page: str | None = None
    visual: str | None = None
    keep_visuals: list[str] = Field(default_factory=list)
    hide_others: bool = True
    filters: list[FilterSpec] = Field(default_factory=list)
    runs: list[RunNode] = Field(default_factory=list)  # empty -> a single run from the fields above
    export_type: ExportType = "Summarized"
    settle_ms: int = 750
    stable_reads: int = 2

    @classmethod
    def load(cls, path: Path) -> Self:
        if not path.exists():
            raise FileNotFoundError(f"config file not found: {path} (copy config.example.yaml to config.yaml)")
        return cls.model_validate(yaml.safe_load(path.read_text()) or {})
