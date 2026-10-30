// Injected into the host page. Exposes window.pbiTool, a thin async API over the
// powerbi-client SDK (2.10.x). Python calls these via page.evaluate and passes plain JSON.
//
// window.pbiTool API (pages/visuals are matched by internal name or title; duplicate titles throw):
//   configure({ wrapper, timeoutMs }) -> sets the embed wrapper selector and page-switch timeout; Python sends it with every call
//                                        (page state is lost on navigation)
//   getReport()                       -> SDK report for the embed wrapper
//   waitReady(embedMs, renderMs)      -> { renders } once powerbi.get finds the report, then it has rendered
//   probe()                           -> which SDK APIs are available
//   listVisuals(pageRef?)             -> [{ page, pageName, name, title, type }]
//   listSlicers(pageRef?)             -> (activates each listed page first: slicer state is only readable on the active page)
//                                        [{ page, pageName, name, title, type, filterType, targets, values, state, stateError? }]
//   setPage(pageRef)                  -> activates a page and waits for it to render
//   hideVisuals(pageRef, keep)        -> hides every visual on the page except keep (custom layout)
//   applyScopes(scopes)               -> sets (replaces) filters per report/page/visual scope
//   exportVisual(pageRef, visualRef, opts) -> { csv, reads, rendered }; page must already be active
//   applySlicers(pageRef, items)      -> { applied: [{ visual, ok, error?, values, filterType }] }; page must already be active
//                                        items: [{ visual, state: { kind: "in", values } | { kind: "raw", state } }]
//   renderCount()                     -> rendered events seen so far (pass as opts.sinceCount)
(() => {
  if (window.pbiTool) return;

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  const state = { wrapperSel: null, timeoutMs: 60000 };

  const RENDER_QUIET_MS = 1500;  // no rendered event for this long counts as settled
  const FIRST_PAGE_RENDER_MS = 5000;  // a page switch that emits no rendered event by then is taken as drawn

  // The SDK raises its events as bubbling CustomEvents on the embed's element. Recording them from
  // document start means a render that happens before we first look up the report is never missed.
  const lifecycles = new WeakMap();
  const lifecycle = (el) => {
    if (!lifecycles.has(el)) lifecycles.set(el, { renders: 0, lastRenderAt: 0, renderedSinceLoad: false });
    return lifecycles.get(el);
  };
  document.addEventListener("loaded", (e) => {
    if (e instanceof CustomEvent) lifecycle(e.target).renderedSinceLoad = false;  // (re-)embedded: not drawn yet
  }, true);
  document.addEventListener("rendered", (e) => {
    if (!(e instanceof CustomEvent)) return;
    const s = lifecycle(e.target);
    s.renders += 1;
    s.lastRenderAt = performance.now();
    s.renderedSinceLoad = true;
  }, true);

  // `models` (powerbi-models enums) is bundled with the SDK, which loads after this script.
  const sdkModels = () => window["powerbi-client"].models;
  const exportType = (name) => {
    const type = sdkModels().ExportDataType[name];
    if (type === undefined) throw new Error(`Unknown export type: ${name}`);
    return type;
  };

  function configure({ wrapper, timeoutMs }) {
    state.wrapperSel = wrapper;
    state.timeoutMs = timeoutMs;
  }

  function wrapperEl() {
    if (!state.wrapperSel) throw new Error("No embed wrapper selector configured; call configure first");
    const el = document.querySelector(state.wrapperSel);
    if (!el) throw new Error(`Embed wrapper not found: ${state.wrapperSel}`);
    return el;
  }

  function getReport() {
    const el = wrapperEl();
    if (!window.powerbi) throw new Error("window.powerbi is not defined");
    const report = window.powerbi.get(el);
    if (!report) throw new Error("powerbi.get() returned nothing for the wrapper");
    return report;
  }

  // Polls check() until it returns something truthy; check may throw while not ready yet.
  async function waitFor(check, timeoutMs, what) {
    const deadline = performance.now() + timeoutMs;
    let lastErr;
    while (performance.now() < deadline) {
      try {
        const result = check();
        if (result) return result;
      } catch (e) { lastErr = e; }
      await sleep(250);
    }
    throw new Error(`${what} not ready after ${timeoutMs}ms${lastErr ? `: ${lastErr.message}` : ""}`);
  }

  async function waitReady(embedMs = 60000, renderMs = 60000) {
    await waitFor(getReport, embedMs, "Report embed (powerbi.get)");
    const s = await waitFor(() => {
      const s = lifecycle(wrapperEl());
      return s.renderedSinceLoad && performance.now() - s.lastRenderAt >= RENDER_QUIET_MS && s;
    }, renderMs, "Report render");
    return { renders: s.renders };
  }

  // Prefers an exact internal-name match; otherwise the title must match exactly one item.
  function pickUnique(items, ref, kind, titleKey) {
    const byName = items.filter((i) => i.name === ref);
    const matches = byName.length ? byName : items.filter((i) => i[titleKey] === ref);
    if (matches.length === 1) return matches[0];
    const problem = matches.length ? `is ambiguous (${matches.length} matches; use the internal name)` : "not found";
    throw new Error(`${kind} "${ref}" ${problem}. Available: ${items.map((i) => `${i[titleKey] || i.name} [${i.name}]`).join(", ")}`);
  }

  async function findPage(report, pageRef) {
    return pickUnique(await report.getPages(), pageRef, "Page", "displayName");
  }

  async function findVisual(page, visualRef) {
    return pickUnique(await page.getVisuals(), visualRef, "Visual", "title");
  }

  // ---- discovery ---------------------------------------------------------

  async function probe() {
    const caps = { sdk: {}, report: {}, page: {}, visual: {}, events: {} };
    const ns = window["powerbi-client"];
    caps.sdk.namespaceKeys = ns ? Object.keys(ns) : null;
    caps.sdk.version = (window.powerbi && window.powerbi.hpm.defaultHeaders["x-sdk-version"]) || null;
    caps.sdk.serviceClass = window.powerbi ? window.powerbi.constructor.name : null;
    caps.sdk.modelsEnums = ["ExportDataType", "VisualContainerDisplayMode", "LayoutType", "FilterType"]
      .reduce((o, k) => ((o[k] = !!(ns && ns.models && ns.models[k])), o), {});
    caps.sdk.scriptSrcs = Array.from(document.scripts).map((s) => s.src).filter(Boolean);

    const report = getReport();
    const has = (o, k) => typeof o[k] === "function";
    for (const k of ["getPages", "getFilters", "setFilters", "updateSettings", "on", "off"]) {
      caps.report[k] = has(report, k);
    }
    const pages = await report.getPages();
    caps.page.count = pages.length;
    const page = pages[0];
    for (const k of ["setActive", "getVisuals", "setFilters", "getFilters", "hasLayout"]) caps.page[k] = has(page, k);
    if (has(page, "getVisuals")) {
      const visuals = await page.getVisuals();
      caps.visual.count = visuals.length;
      if (visuals[0]) for (const k of ["exportData", "setFilters", "getFilters"]) caps.visual[k] = has(visuals[0], k);
    }
    caps.events.supportsOn = caps.report.on;
    await probeSlicers(pages, caps);
    return caps;
  }

  const isSlicer = (v) => /slicer/i.test(v.type || "");
  const BASIC_FILTER = 1;

  async function probeSlicers(pages, caps) {
    caps.slicerCount = 0;
    try {
      const slicers = [];
      let sample;  // state is only readable on the active page; other pages' visuals are not initialised
      for (const p of pages) {
        const found = (await p.getVisuals()).filter(isSlicer);
        slicers.push(...found);
        if (p.isActive && found.length) sample ??= found[0];
      }
      caps.slicerCount = slicers.length;
      caps.slicerTypes = [...new Set(slicers.map((v) => v.type))];
      if (!slicers.length) return;
      caps.visual.getSlicerState = typeof slicers[0].getSlicerState === "function";
      caps.visual.setSlicerState = typeof slicers[0].setSlicerState === "function";
      if (caps.visual.getSlicerState && sample) caps.sampleSlicerState = await sample.getSlicerState();
    } catch (e) { caps.slicerError = String(e.message || e); }
  }

  function summariseSlicerState(state) {
    const filter = (state.filters || [])[0];
    const filterType = filter ? filter.filterType : null;
    const isIn = filterType === BASIC_FILTER && filter.operator === "In";
    const values = isIn ? filter.values : (filter ? null : []);
    return { filterType, targets: state.targets, values };
  }

  async function listSlicers(pageRef) {
    const report = getReport();
    const pages = pageRef ? [await findPage(report, pageRef)] : await report.getPages();
    const out = [];
    for (const p of pages) {
      await activate(p.name);
      for (const v of (await p.getVisuals()).filter(isSlicer)) {
        const row = { page: p.displayName, pageName: p.name, name: v.name, title: v.title || "", type: v.type || "" };
        try {
          const state = await v.getSlicerState();
          out.push({ ...row, ...summariseSlicerState(state), state });
        } catch (e) {
          out.push({ ...row, filterType: null, targets: null, values: null, state: null, stateError: String(e.message || e) });
        }
      }
    }
    return out;
  }

  async function listVisuals(pageRef) {
    const report = getReport();
    const pages = pageRef ? [await findPage(report, pageRef)] : await report.getPages();
    const out = [];
    for (const p of pages) {
      const visuals = await p.getVisuals();
      for (const v of visuals) {
        out.push({ page: p.displayName, pageName: p.name, name: v.name, title: v.title || "", type: v.type || "" });
      }
    }
    return out;
  }

  // ---- actions -----------------------------------------------------------

  // A page that is not active (or not yet rendered) has uninitialised visuals: getSlicerState fails with
  // "visual config not initialized". So switching pages waits for the new page to render.
  // Looks the page up afresh: setActive does not update isActive on Page objects fetched earlier.
  async function activate(pageRef) {
    const page = await findPage(getReport(), pageRef);
    if (page.isActive) return page;
    const sinceCount = lifecycle(wrapperEl()).renders;
    await page.setActive();
    await waitSettled({ sinceCount, settleMs: RENDER_QUIET_MS, firstRenderMs: FIRST_PAGE_RENDER_MS, timeoutMs: state.timeoutMs });
    return page;
  }

  async function setPage(pageRef) {
    return { page: (await activate(pageRef)).displayName };
  }

  // Show the visuals in keep (matched by name or title) and hide every other visual on the page.
  // This SDK has no setVisualDisplayState; visibility goes through a custom page layout.
  async function hideVisuals(pageRef, keep) {
    const report = getReport();
    const page = await findPage(report, pageRef);
    const { VisualContainerDisplayMode: Mode, LayoutType } = sdkModels();
    const visualsLayout = {};
    const hidden = [];
    for (const v of await page.getVisuals()) {
      const shown = keep.includes(v.name) || keep.includes(v.title);
      visualsLayout[v.name] = { displayState: { mode: shown ? Mode.Visible : Mode.Hidden } };
      if (!shown) hidden.push(v.title || v.name);
    }
    await report.updateSettings({
      layoutType: LayoutType.Custom,
      customLayout: { pagesLayout: { [page.name]: { visualsLayout } } },
    });
    return { hidden };
  }

  // scopes: [{ level: "report"|"page"|"visual", page, visual, filters: [<SDK filter JSON>] }]
  // setFilters replaces everything at that scope, so an empty list clears it. Note this also clears
  // any filters the host site applied at the same scope.
  async function applyScopes(scopes) {
    const report = getReport();
    for (const s of scopes) {
      if (s.level === "report") await report.setFilters(s.filters);
      else if (s.level === "page") await (await findPage(report, s.page)).setFilters(s.filters);
      else if (s.level === "visual") {
        const page = await findPage(report, s.page);
        await (await findVisual(page, s.visual)).setFilters(s.filters);
      } else throw new Error(`Unknown filter level: ${s.level}`);
    }
    return { scopes: scopes.length };
  }

  // Sets slicer states one at a time (the report is slow). An empty "in" list resets the slicer.
  // The page must already be active; render freshness is the caller's job (waitSettled).
  async function applySlicers(pageRef, items) {
    const report = getReport();
    const page = await findPage(report, pageRef);
    const applied = [];
    for (const item of items) {
      const visual = await findVisual(page, item.visual);
      try {
        let next;
        if (item.state.kind === "raw") next = item.state.state;
        else {
          const { targets } = await visual.getSlicerState();
          next = {
            targets,
            filters: [{
              $schema: "http://powerbi.com/product/schema#basic",
              target: targets[0], operator: "In", values: item.state.values, filterType: BASIC_FILTER,
            }],
          };
        }
        await visual.setSlicerState(next);
        const { filterType, values } = summariseSlicerState(await visual.getSlicerState());
        applied.push({ visual: item.visual, ok: true, values, filterType });
      } catch (e) {
        applied.push({ visual: item.visual, ok: false, error: String(e.message || e), values: null, filterType: null });
      }
    }
    return { applied };
  }

  // ---- freshness + export ------------------------------------------------

  // Resolves once at least one render has happened since `sinceCount`, and then the report has been
  // quiet for settleMs. If the SDK never emits (or nothing re-renders because the filter was a no-op),
  // gives up waiting for the first render after firstRenderMs and relies on the data-stability check.
  async function waitSettled({ sinceCount, settleMs, firstRenderMs, timeoutMs }) {
    const s = lifecycle(wrapperEl());
    const start = performance.now();
    while (performance.now() - start < timeoutMs) {
      const rendered = s.renders > sinceCount;
      const quiet = performance.now() - s.lastRenderAt >= settleMs;
      if (rendered && quiet) return { rendered: true };
      if (!rendered && performance.now() - start > firstRenderMs) return { rendered: false };
      await sleep(100);
    }
    throw new Error(`Report did not settle within ${timeoutMs}ms`);
  }

  // Export the visual's data, only returning once two consecutive reads agree.
  // sinceCount should be captured BEFORE the change that is expected to re-render (see Python caller).
  async function exportVisual(pageRef, visualRef, opts = {}) {
    const {
      type = "Summarized", settleMs = 750, firstRenderMs = 5000,
      timeoutMs = 60000, stableReads = 2, pollMs = 500, sinceCount = lifecycle(wrapperEl()).renders,
    } = opts;
    const report = getReport();
    const page = await findPage(report, pageRef);
    const visual = await findVisual(page, visualRef);

    const deadline = performance.now() + timeoutMs;
    const settled = await waitSettled({ sinceCount, settleMs, firstRenderMs, timeoutMs });

    let prev = null, agree = 0, reads = 0;
    while (performance.now() < deadline) {
      const res = await visual.exportData(exportType(type));
      reads += 1;
      const data = res && res.data;
      if (typeof data !== "string") throw new Error("exportData returned no CSV text");
      agree = data === prev ? agree + 1 : 1;
      prev = data;
      if (agree >= stableReads) return { csv: data, reads, rendered: settled.rendered };
      await sleep(pollMs);
    }
    throw new Error(`Export never stabilised within ${timeoutMs}ms (${reads} reads) - refusing to return possibly stale data`);
  }

  window.pbiTool = {
    configure, getReport, waitReady, probe, listVisuals, listSlicers, applySlicers, setPage, hideVisuals, applyScopes, exportVisual,
    renderCount: () => lifecycle(wrapperEl()).renders,
  };
})();
