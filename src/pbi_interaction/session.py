"""Browser lifecycle, login, and the Python -> window.pbiTool bridge."""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from importlib import resources
from typing import Any, NamedTuple, Self

from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright
from playwright.sync_api import Error as PlaywrightError

from .errors import AuthError, PbiError
from .runs import ResolvedSlicer, Run, ScopeKey
from .settings import BASIC_FILTER_TYPE, RunConfig, Settings, SlicerRef

log = logging.getLogger(__name__)


class Export(NamedTuple):
    csv: str
    rendered: bool  # a render was observed after this run's changes


SlicerKey = tuple[str, str]  # (page, slicer id)
Values = tuple[str, ...]


def _norm(values: Any) -> Values | None:
    """Order-insensitive values; None means unknown (non-Basic or unreadable state), never an empty selection."""
    return None if values is None else tuple(sorted(str(v) for v in values))


def _show(values: Values | None) -> list[str] | str:
    return "unknown" if values is None else list(values)


def _pick(refs: list[SlicerRef], page: str) -> SlicerRef | None:
    return next((r for r in refs if r.page == page), None) or next((r for r in refs if r.page is None), None)


def _find(live: list[dict], ref: str) -> dict:
    """Match a raw slicer ref by visual id, else by unique title."""
    if match := next((s for s in live if s["name"] == ref), None):
        return match
    by_title = [s for s in live if s.get("title") == ref]
    if len(by_title) == 1:
        return by_title[0]
    kind = "is ambiguous" if by_title else "matches no slicer"
    candidates = ", ".join(f"{s.get('title')} [{s['name']}]" for s in by_title or live)
    raise LookupError(f"slicer '{ref}' {kind}; candidates: {candidates}")


def _tool_js() -> str:
    return resources.files("pbi_interaction").joinpath("js/pbi_tool.js").read_text()


class PbiSession:
    def __init__(self, settings: Settings, cfg: RunConfig, headed: bool = False):
        self.settings, self.cfg = settings, cfg
        self.headless = settings.headless and not headed
        self._touched: set[ScopeKey] = set()
        self._slicer_applied: dict[SlicerKey, Values | None] = {}
        self._slicer_catalog: dict[str, list[dict]] = {}
        self._slicer_set: set[SlicerKey] = set()  # set by an earlier run: reset to default once unused
        self.page: Page
        self._context: BrowserContext
        self._browser: Browser

    # ---- lifecycle -------------------------------------------------------

    @contextmanager
    def open(self) -> Iterator[Self]:
        with sync_playwright() as p:
            self._browser = p.chromium.launch(headless=self.headless)
            try:
                self._start_context()
                self._ensure_logged_in()
                yield self
            except Exception:
                self.debug_dump("failure")
                raise
            finally:
                self._browser.close()

    def _start_context(self) -> None:
        state = self.settings.state_path
        kwargs: dict[str, Any] = {}
        if state.exists():
            kwargs["storage_state"] = str(state)
        self._context = self._browser.new_context(**kwargs)
        self._context.set_default_timeout(self.settings.timeout_s * 1000)
        self._context.add_init_script(_tool_js())
        self.page = self._context.new_page()

    # ---- auth ------------------------------------------------------------

    def _embed_present(self, timeout_s: float) -> bool:
        try:
            self.page.wait_for_selector(self.cfg.embed_wrapper, state="attached", timeout=timeout_s * 1000)
            return True
        except PlaywrightError:
            return False

    def _ensure_logged_in(self) -> None:
        self.page.goto(self.settings.site_url)
        state = self.settings.state_path
        if state.exists():
            if self._saved_session_valid():
                log.info("reused saved session")
                return
            log.info("saved session is no longer valid; removing it")
            state.unlink()
        self._login()

    def _saved_session_valid(self) -> bool:
        """Wait for the embed or the login form; only a visible login form invalidates the session."""
        embed = self.page.locator(self.cfg.embed_wrapper)
        login = self.page.locator(self.cfg.login.username)
        try:
            embed.or_(login).first.wait_for(state="attached", timeout=self.settings.timeout_s * 1000)
        except PlaywrightError:
            pass  # neither appeared: handled below
        if embed.count():
            return True
        if login.is_visible():
            return False
        raise AuthError("Neither the embedded report nor the login form appeared; saved session kept")

    def _login(self) -> None:
        log.info("logging in as %s", self.settings.username)
        sel = self.cfg.login
        password = self.settings.resolved_password().get_secret_value()
        self.page.fill(sel.username, self.settings.username)
        self.page.fill(sel.password, password)
        self.page.click(sel.submit)
        # Exactly one attempt: retrying a bad password risks locking the account.
        if not self._embed_present(self.settings.timeout_s):
            raise AuthError("Login did not reach the embedded report; check credentials/selectors")
        self.wait_ready()  # the report loading proves the auth redirects have settled
        state = self.settings.state_path
        state.parent.mkdir(parents=True, exist_ok=True)
        self._context.storage_state(path=str(state))
        state.chmod(0o600)

    # ---- JS bridge -------------------------------------------------------

    def call(self, fn: str, *args: Any) -> Any:
        try:
            return self.page.evaluate(
                "([cfg, fn, args]) => (window.pbiTool.configure(cfg), window.pbiTool[fn](...args))",
                [{"wrapper": self.cfg.embed_wrapper}, fn, list(args)],
            )
        except PlaywrightError as e:
            raise PbiError(f"pbiTool.{fn} failed: {e.message.splitlines()[0]}") from e

    def wait_ready(self) -> None:
        self.call("waitReady", int(self.settings.timeout_s * 1000))

    def probe(self) -> dict:
        self.wait_ready()
        return self.call("probe")

    def list_visuals(self) -> list[dict]:
        self.wait_ready()
        return self.call("listVisuals", self.cfg.page)

    def list_slicers(self, page: str | None = None) -> list[dict]:
        return self.call("listSlicers", page)

    def _registry(self, page: str) -> list[tuple[str, SlicerRef]]:
        entries = ((name, _pick(refs, page)) for name, refs in self.cfg.slicer_names.items())
        return [(name, ref) for name, ref in entries if ref]

    def _kept_slicers(self, page: str) -> list[tuple[str, SlicerRef]]:
        keep = set(self.cfg.keep_visuals)
        return [(name, ref) for name, ref in self._registry(page) if name in keep or ref.id in keep]

    def _visual_id(self, name: str, page: str) -> str:
        return next((ref.id for n, ref in self._registry(page) if n == name), name)

    def validate_slicers(self, runs: list[Run]) -> None:
        """Fail fast, before a batch, if configured slicers are missing, mismatched or misused."""
        problems: list[str] = []
        for page in dict.fromkeys(r.page for r in runs):
            used = [s for r in runs if r.page == page for s in r.slicers]
            kept = {name for name, _ in self._kept_slicers(page)}
            wanted = {s.name for s in used if s.name} | kept
            expected = {n: ref for n, ref in self._registry(page) if ref.page == page or n in wanted}
            if not used and not expected:
                continue
            problems += self._leaking_slicers(page, [r for r in runs if r.page == page])
            self.wait_ready()
            live = self.list_slicers(page)
            by_id = {s["name"]: s for s in live}
            problems += [f"page '{page}': {p}" for p in self._slicer_problems(live, by_id, expected, used)]
        if problems:
            raise PbiError("slicer validation failed:\n" + "\n".join(problems))

    @staticmethod
    def _leaking_slicers(page: str, runs: list[Run]) -> list[str]:
        """A slicer without a default cannot be reset, so every run on the page must set it."""
        key = lambda s: s.id or s.ref  # noqa: E731
        setters = [{key(s) for s in r.slicers if not s.reset} for r in runs]
        always_set = set.intersection(*setters)
        unresettable = {key(s): s for r in runs for s in r.slicers if not s.reset and s.default is None}
        return [
            f"page '{page}': slicer '{s.ref}' has no default to reset to but is not set by every run; "
            "register it in slicer_names with a default"
            for k, s in unresettable.items() if k not in always_set
        ]

    @staticmethod
    def _slicer_problems(
        live: list[dict], by_id: dict[str, dict], expected: dict[str, SlicerRef], used: list[ResolvedSlicer],
    ) -> list[str]:
        problems = []
        for name, ref in expected.items():
            if (s := by_id.get(ref.id)) is None:
                problems.append(f"slicer '{name}' ({ref.id}) not found")
            elif s.get("stateError"):
                problems.append(f"slicer '{name}' state is unreadable: {s['stateError']}")
            elif s["filterType"] != ref.filter_type:
                problems.append(f"slicer '{name}' has filterType {s['filterType']}, expected {ref.filter_type}")
            elif ref.default is not None and _norm(s.get("values")) != _norm(ref.default):
                at, want = _show(_norm(s.get("values"))), _show(_norm(ref.default))
                problems.append(f"slicer '{name}' is at {at}, expected default {want}")
        for spec in used:
            try:
                s = by_id.get(spec.id) if spec.id else _find(live, spec.ref)
            except LookupError as e:
                problems.append(str(e))
                continue
            if s and spec.values is not None and s["filterType"] != BASIC_FILTER_TYPE:
                problems.append(f"slicer '{spec.ref}' is not a Basic slicer; it cannot be set to values")
        return problems

    def _sync_slicers(self, page: str) -> None:
        """Read the page's slicers; record live values for any slicer not already known."""
        live = self._slicer_catalog[page] = self.list_slicers(page)
        for s in live:
            self._slicer_applied.setdefault((page, s["name"]), _norm(s.get("values")))

    def _desired_slicers(self, run: Run) -> tuple[dict[str, dict], list[str]]:
        """Target state per slicer id for this run, and the ids the run itself sets."""
        desired: dict[str, dict] = {}
        for name, ref in self._kept_slicers(run.page):
            if ref.default is None:
                log.warning("kept slicer '%s' has no default; leaving it as is", name)
            else:
                desired[ref.id] = {"kind": "in", "values": list(ref.default)}
        for name, ref in self._registry(run.page):
            if (run.page, ref.id) in self._slicer_set and ref.id not in desired:
                if ref.default is None:
                    log.warning("slicer '%s' was set earlier but has no default; leaving it as is", name)
                else:
                    desired[ref.id] = {"kind": "in", "values": list(ref.default)}
        own = []
        for spec in run.slicers:
            try:
                sid = spec.id or _find(self._slicer_catalog[run.page], spec.ref)["name"]
            except LookupError as e:
                raise PbiError(str(e)) from e
            if spec.raw is not None:
                desired[sid] = {"kind": "raw", "state": spec.raw}
            else:
                values = spec.default if spec.reset else spec.values
                if values is None:
                    raise PbiError(f"slicer '{spec.ref}' has no default to reset to")
                desired[sid] = {"kind": "in", "values": list(values)}
            own.append(sid)
        self._slicer_set |= {(run.page, sid) for sid in own}
        return desired, own

    def _forget_other_pages(self, page: str) -> None:
        """Synced slicers can change on other pages too, so their cached state is stale after an apply."""
        self._slicer_applied = {k: v for k, v in self._slicer_applied.items() if k[0] == page}
        self._slicer_catalog = {p: c for p, c in self._slicer_catalog.items() if p == page}

    def _apply_slicers(self, run: Run) -> list[str]:
        """Send only the slicers whose state differs from the last known; returns ids the run sets."""
        page = run.page
        if page not in self._slicer_catalog:
            self._sync_slicers(page)
        desired, own = self._desired_slicers(run)
        if any((page, sid) not in self._slicer_applied for sid in desired):
            self._sync_slicers(page)
        changes = {
            sid: state for sid, state in desired.items()
            if state["kind"] == "raw" or self._slicer_applied.get((page, sid)) != _norm(state["values"])
        }
        if not changes:
            return own
        for sid in changes:  # unknown until read back: a partial failure re-syncs next run
            self._slicer_applied.pop((page, sid), None)
        self._forget_other_pages(page)
        res = self.call("applySlicers", page, [{"visual": sid, "state": st} for sid, st in changes.items()])
        results = {r["visual"]: r for r in res["applied"]}
        failures = []
        for sid, state in changes.items():
            r = results.get(sid, {"ok": False, "error": "no result"})
            actual = _norm(r.get("values"))
            if r.get("ok") and (state["kind"] == "raw" or actual == _norm(state["values"])):
                self._slicer_applied[(page, sid)] = actual
            else:
                requested = state["state"] if state["kind"] == "raw" else list(_norm(state["values"]))
                detail = f"slicer {sid}: requested {requested}, got {_show(actual)} {r.get('error', '')}"
                failures.append(detail.rstrip())
        if failures:
            raise PbiError("slicer apply failed: " + "; ".join(failures))
        return own

    def run_one(self, run: Run) -> Export:
        """Navigate, set slicers, show/hide, set filters, wait for fresh data, return the exported CSV.

        Scopes touched by earlier runs but not this one are reset to no filters, so filters
        never leak from one run into the next, even after a failed run.
        """
        self.wait_ready()
        # Mark the render counter BEFORE any change so only renders caused by this run count.
        since = self.call("renderCount")
        self.call("setPage", run.page)
        own_slicers = self._apply_slicers(run) if self.cfg.slicer_names or run.slicers else []
        if self.cfg.hide_others:
            keep = [self._visual_id(v, run.page) for v in self.cfg.keep_visuals]
            res = self.call("hideVisuals", run.page, [run.visual, *keep, *own_slicers])
            log.debug("hide visuals: %s", res)
        scopes = run.scopes()
        self._touched |= set(scopes)  # recorded before applying: a partial failure may leave filters set
        payload = [
            {"level": lvl, "page": pg, "visual": vis, "filters": scopes.get(key, [])}
            for key in sorted(self._touched, key=str)
            for lvl, pg, vis in [key]
        ]
        if payload:
            self.call("applyScopes", payload)
        self._touched = set(scopes)
        res = self.call(
            "exportVisual", run.page, run.visual,
            {
                "type": run.export_type,
                "settleMs": self.cfg.settle_ms,
                "stableReads": self.cfg.stable_reads,
                "timeoutMs": int(self.settings.timeout_s * 1000),
                "sinceCount": since,
            },
        )
        log.info("export ok: %d reads, render observed=%s", res["reads"], res["rendered"])
        return Export(res["csv"], res["rendered"])

    # ---- debugging -------------------------------------------------------

    def debug_dump(self, label: str) -> None:
        try:
            d = self.settings.debug_dir
            d.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            self.page.screenshot(path=str(d / f"{label}_{stamp}.png"))
            log.error("saved failure screenshot to %s", d)
        except (PlaywrightError, OSError):  # never mask the original error
            pass
