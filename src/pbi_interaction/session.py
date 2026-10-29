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
from .runs import Run, ScopeKey
from .settings import RunConfig, Settings

log = logging.getLogger(__name__)


class Export(NamedTuple):
    csv: str
    rendered: bool  # a render was observed after this run's changes


def _tool_js() -> str:
    return resources.files("pbi_interaction").joinpath("js/pbi_tool.js").read_text()


class PbiSession:
    def __init__(self, settings: Settings, cfg: RunConfig, headed: bool = False):
        self.settings, self.cfg = settings, cfg
        self.headless = settings.headless and not headed
        self._touched: set[ScopeKey] = set()
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
            if self._embed_present(10):
                log.info("reused saved session")
                return
            log.info("saved session is no longer valid; removing it")
            state.unlink()
        self._login()

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
        state = self.settings.state_path
        state.parent.mkdir(parents=True, exist_ok=True)
        self._context.storage_state(path=str(state))
        state.chmod(0o600)

    # ---- JS bridge -------------------------------------------------------

    def call(self, fn: str, *args: Any) -> Any:
        try:
            return self.page.evaluate("([fn, args]) => window.pbiTool[fn](...args)", [fn, list(args)])
        except PlaywrightError as e:
            raise PbiError(f"pbiTool.{fn} failed: {e.message.splitlines()[0]}") from e

    def wait_ready(self) -> None:
        self.call("waitReady", self.cfg.embed_wrapper, int(self.settings.timeout_s * 1000))

    def probe(self) -> dict:
        self.wait_ready()
        return self.call("probe", self.cfg.embed_wrapper)

    def list_visuals(self) -> list[dict]:
        self.wait_ready()
        return self.call("listVisuals", self.cfg.page)

    def run_one(self, run: Run) -> Export:
        """Navigate, show/hide, set filters, wait for fresh data, return the exported CSV.

        Scopes touched by earlier runs but not this one are reset to no filters, so filters
        never leak from one run into the next, even after a failed run.
        """
        self.wait_ready()
        # Mark the render counter BEFORE any change so only renders caused by this run count.
        since = self.call("renderCount")
        self.call("setPage", run.page)
        if self.cfg.hide_others:
            res = self.call("hideVisuals", run.page, [run.visual, *self.cfg.keep_visuals])
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
