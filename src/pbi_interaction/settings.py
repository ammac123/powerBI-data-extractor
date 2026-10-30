"""Secrets and environment config (env vars > .env > defaults) plus the YAML run config."""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Annotated, Any, Literal, Self

import keyring
import yaml
from pydantic import BaseModel, ConfigDict, Discriminator, Field, SecretStr, Tag, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .errors import AuthError
from .sheetnames import check_sheet_name

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


class SlicerRef(BaseModel):
    """A slicer visual registered under a friendly name.

    `default` snapshots the slicer's default values ([] = no selection, None = unknown).
    `values`, when given, lists the valid values and is enforced on configured values.
    """

    id: str
    page: str | None = None
    filter_type: int = BASIC_FILTER_TYPE
    default: list[Any] | None = None
    values: list[Any] | None = None

    @model_validator(mode="after")
    def _check_values(self) -> Self:
        if self.values == []:
            raise ValueError(f"slicer '{self.id}': empty valid values; omit values to skip checking")
        return self

    def invalid(self, values: list[Any]) -> list[Any]:
        """The given values that are not among this slicer's valid values."""
        if self.values is None:
            return []
        valid = {str(v) for v in self.values}
        return [v for v in values if str(v) not in valid]


class SlicerSpec(BaseModel):
    """Set a slicer to `values`, apply a `raw` SDK filter, or `reset` it to its default."""

    model_config = ConfigDict(extra="forbid")

    slicer: str  # friendly name, raw visual id or title
    values: list[Any] | None = None
    raw: dict[str, Any] | None = None
    reset: bool = False

    @model_validator(mode="before")
    @classmethod
    def _scalar_values(cls, data: Any) -> Any:
        if isinstance(data, dict) and data.get("values") is not None and not isinstance(data["values"], list):
            return {**data, "values": [data["values"]]}
        return data

    @model_validator(mode="after")
    def _check_shape(self) -> Self:
        if self.values == []:
            raise ValueError(f"slicer '{self.slicer}': empty values; use null to reset to default")
        if [self.values is not None, self.raw is not None, self.reset].count(True) != 1:
            raise ValueError(f"slicer '{self.slicer}': needs exactly one of values, raw or reset")
        return self


def _slicer_specs(value: Any) -> Any:
    """Normalise the `{name: value}` shorthand into SlicerSpec dicts; null means reset."""
    if not isinstance(value, dict):
        return value
    def shape(v: Any) -> dict[str, Any]:
        if v is None:
            return {"reset": True}
        return {"raw": v} if isinstance(v, dict) else {"values": v}

    return [{"slicer": name, **shape(v)} for name, v in value.items()]


def _axis_kind(axis: Any) -> str:
    has_slicer = "slicer" in axis if isinstance(axis, dict) else isinstance(axis, SlicerSpec)
    return "slicer" if has_slicer else "filter"


MatrixAxis = Annotated[
    Annotated[FilterSpec, Tag("filter")] | Annotated[SlicerSpec, Tag("slicer")], Discriminator(_axis_kind)
]


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
    slicers: list[SlicerSpec] = Field(default_factory=list)
    matrix: list[MatrixAxis] = Field(default_factory=list)
    children: list[RunNode] = Field(default_factory=list)

    _normalise_slicers = field_validator("slicers", mode="before")(_slicer_specs)

    @model_validator(mode="after")
    def _check_slicer_axes(self) -> Self:
        for i, axis in enumerate(self.matrix):
            if not isinstance(axis, SlicerSpec):
                continue
            if axis.values is None:
                raise ValueError(f"matrix slicer '{axis.slicer}': axis needs values, not raw or reset")
            unique = {json.dumps(v, sort_keys=True, default=str): v for v in axis.values}
            self.matrix[i] = axis.model_copy(update={"values": list(unique.values())})
        refs = [a.slicer for a in self.matrix if isinstance(a, SlicerSpec)]
        if len(set(refs)) != len(refs):
            raise ValueError(f"run '{self.label}': more than one matrix axis on the same slicer")
        return self


class SheetSpec(BaseModel):
    """`sheet`: one worksheet per matched run. `aggregate`: matched runs share a sheet, joined on `key`."""

    name: str
    runs: list[str]  # fnmatch globs over run labels
    mode: Literal["sheet", "aggregate"] = "sheet"
    key: list[str] = Field(default_factory=list)
    layout: Literal["merged", "blocks"] = "merged"  # merged: one outer-joined table; blocks: a table per run

    @model_validator(mode="after")
    def _check(self) -> Self:
        check_sheet_name(self.name)
        if self.mode == "aggregate" and not self.key:
            raise ValueError(f"sheet '{self.name}': aggregate needs a key")
        return self


class WorkbookSpec(BaseModel):
    name: str
    sheets: list[SheetSpec]
    unassigned: Literal["sheet", "ignore", "error"] = "sheet"  # runs matched by no sheet

    @model_validator(mode="after")
    def _unique_sheets(self) -> Self:
        names = [s.name.lower() for s in self.sheets]
        if len(set(names)) != len(names):
            raise ValueError(f"workbook '{self.name}': duplicate sheet names")
        return self


class RunConfig(BaseModel):
    embed_wrapper: str = "#power-bi-iframe-wrapper"
    login: LoginSelectors = LoginSelectors()
    page: str | None = None
    visual: str | None = None
    keep_visuals: list[str] = Field(default_factory=list)
    hide_others: bool = True
    filters: list[FilterSpec] = Field(default_factory=list)
    slicer_names: dict[str, list[SlicerRef]] = Field(default_factory=dict)
    slicers: list[SlicerSpec] = Field(default_factory=list)
    runs: list[RunNode] = Field(default_factory=list)  # empty -> a single run from the fields above
    export_type: ExportType = "Summarized"
    settle_ms: int = 750
    stable_reads: int = 2
    workbooks: list[WorkbookSpec] = Field(default_factory=list)

    _normalise_slicers = field_validator("slicers", mode="before")(_slicer_specs)

    @field_validator("slicer_names", mode="before")
    @classmethod
    def _normalise_slicer_names(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value

        def wrap(e: Any) -> Any:
            return {"id": e} if isinstance(e, str) else e

        entries = {name: entry if isinstance(entry, list) else [entry] for name, entry in value.items()}
        return {name: [wrap(e) for e in entry] for name, entry in entries.items()}

    @model_validator(mode="after")
    def _check_slicer_names(self) -> Self:
        seen: dict[tuple[str | None, str], str] = {}
        for name, refs in self.slicer_names.items():
            if not refs:
                raise ValueError(f"slicer '{name}': needs at least one entry")
            pages = [r.page for r in refs]
            if len(set(pages)) != len(pages):
                raise ValueError(f"slicer '{name}': more than one entry for the same page")
            if None in pages and len(pages) > 1:
                raise ValueError(f"slicer '{name}': mixes an entry without a page with paged entries")
            for ref in refs:
                other = seen.setdefault((ref.page, ref.id), name)
                if other != name:
                    raise ValueError(f"slicers '{other}' and '{name}' share page and id")
        for spec in self._configured_slicers():
            self._check_slicer_values(spec)
        return self

    def _configured_slicers(self) -> Iterator[SlicerSpec]:
        def walk(nodes: list[RunNode]) -> Iterator[SlicerSpec]:
            for node in nodes:
                yield from node.slicers
                yield from (a for a in node.matrix if isinstance(a, SlicerSpec))
                yield from walk(node.children)

        yield from self.slicers
        yield from walk(self.runs)

    def _check_slicer_values(self, spec: SlicerSpec) -> None:
        refs = self.slicer_names.get(spec.slicer, [])
        if spec.values is not None and any(r.filter_type != BASIC_FILTER_TYPE for r in refs):
            raise ValueError(f"slicer '{spec.slicer}': only Basic slicers accept values; use raw or null")
        if spec.values is not None and refs and all(r.invalid(spec.values) for r in refs):
            bad = refs[0].invalid(spec.values)[0]
            raise ValueError(f"slicer '{spec.slicer}': value {bad!r} is not in its valid values")

    @model_validator(mode="after")
    def _unique_workbooks(self) -> Self:
        names = [w.name for w in self.workbooks]
        if len(set(names)) != len(names):
            raise ValueError("duplicate workbook names")
        return self

    @classmethod
    def load(cls, path: Path) -> Self:
        if not path.exists():
            raise FileNotFoundError(f"config file not found: {path} (copy config.example.yaml to config.yaml)")
        return cls.model_validate(yaml.safe_load(path.read_text()) or {})
