"""Browser lifecycle, login, and the Python -> window.pbiTool bridge."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from datetime import datetime
from importlib import resources
from pathlib import Path
from typing import Any, Iterator

from playwright.sync_api import Browser, BrowserContext, Error as PlaywrightError, Page, sync_playwright

from .runs import Run
from .settings import RunConfig, Settings

log = logging.getLogger("pbi")


class AuthError(RuntimeError):
    pass


class PbiError(RuntimeError):
    pass


def _tool_js() -> str:
    return resources.files("pbi_interaction").joinpath("js/pbi_tool.js").read_text()


class PbiSession:
    def __init__(self, settings: Settings, cfg: RunConfig, headed: bool = False):
        self.settings, self.cfg = settings, cfg
        self.headless = settings.headless and not headed
        self.page: Page
        self._context: BrowserContext
        self._browser: Browser

    # ---- lifecycle -------------------------------------------------------

    @contextmanager
    def open(self) -> Iterator["PbiSession"]:
        with sync_playwright() as p:
            self._browser = p.chromium.launch(headless=self.headless)
            try:
                self._start_context()
                self._login_if_needed()
                yield self
            except Exception:
                self._debug_dump("failure")
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

    def _login_if_needed(self) -> None:
        self.page.goto(self.settings.site_url)
        # A valid saved session lands straight on the embed.
        if self.settings.state_path.exists() and self._embed_present(10):
            log.info("reused saved session")
            return
        self._login()

    def _login(self) -> None:
        log.info("logging in as %s", self.settings.username)
        sel = self.cfg.login
        password = self.settings.resolved_password().get_secret_value()
        self.page.goto(self.settings.site_url)
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

    def run_one(self, run: Run, prev_keys: set) -> tuple[str, set]:
        """Navigate, show/hide, set filters, wait for fresh data, return (CSV text, filter scopes used).

        Scopes touched by the previous run but not this one are reset to no filters, so filters
        never leak from one run into the next.
        """
        self.wait_ready()
        # Mark the render counter BEFORE any change so only renders caused by this run count.
        since = self.call("renderCount")
        self.call("setPage", run.page)
        if self.cfg.hide_others:
            res = self.call("hideVisuals", run.page, [run.visual, *self.cfg.keep_visuals])
            log.debug("hide visuals: %s", res)
        scopes = run.scopes()
        keys = set(scopes)
        payload = [
            {"level": lvl, "page": pg, "visual": vis, "filters": scopes.get(k, [])}
            for k in sorted(keys | prev_keys, key=str)
            for lvl, pg, vis in [k]
        ]
        if payload:
            self.call("applyScopes", payload)
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
        return res["csv"], keys

    # ---- debugging -------------------------------------------------------

    def _debug_dump(self, label: str) -> None:
        try:
            d = self.settings.debug_dir
            d.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            self.page.screenshot(path=str(d / f"{label}_{stamp}.png"))
            log.error("saved failure screenshot to %s", d)
        except Exception:  # never mask the original error
            pass
