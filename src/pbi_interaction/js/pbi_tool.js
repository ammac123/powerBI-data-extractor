// Injected into the host page. Exposes window.pbiTool, a thin async API over the
// powerbi-client SDK (2.10.x). Python calls these via page.evaluate and passes plain JSON.
//
// window.pbiTool API (pages/visuals are matched by internal name or title; duplicate titles throw):
//   invoke(cfg, fn, args)             -> configure(cfg) then fn(...args), rejected once cfg.budgetMs runs out
//                                        (SDK calls have no timeout of their own). Python calls everything through it.
//   configure({ wrapper, budgetMs, settleMs }) -> embed wrapper selector, time left for this call, render grace period
//   getReport()                       -> SDK report for the embed wrapper
//   waitReady()                       -> { renders } once powerbi.get finds the report, then it has rendered
//   probe()                           -> which SDK APIs are available
//   listVisuals(pageRef?)             -> [{ page, pageName, name, title, type }]
//   listSlicers(pageRef?)             -> (activates each listed page first: slicer state is only readable on the active page)
//                                        [{ page, pageName, name, title, type, filterType, targets, values, state, stateError? }]
//   setPage(pageRef)                  -> activates a page and waits for it to render
//   hideVisuals(pageRef, keep)        -> hides every visual on the page except keep (custom layout)
//   applyScopes(scopes)               -> sets (replaces) filters per report/page/visual scope
//   applySlicers(pageRef, items)      -> { applied: [{ visual, ok, error?, values, filterType }] }; page must already be active
//                                        items: [{ visual, state: { kind: "in", values } | { kind: "raw", state } }]
//   exportVisual(pageRef, visualRef, opts) -> { csv, reads, rendered }; waits for pending changes to render first
//
// Settling: every command that changes data (page switch, filters, slicers, showing a hidden visual) marks the report
// as changed once the SDK has accepted it. The report fires one `rendered` after all pending changes finish, so a
// change has settled once a `rendered` arrives after the last change, followed by settleMs without another one.
(() => {
  if (window.pbiTool) return;

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  const state = { wrapperSel: null, deadline: Infinity, settleMs: 1500 };
  const shownByPage = {};  // page name -> visual names left visible by our last hideVisuals

  // The SDK raises its events as bubbling CustomEvents on the embed's element. Recording them from
  // document start means a render that happens before we first look up the report is never missed.
  const lifecycles = new WeakMap();
  const lifecycle = (el) => {
    if (!lifecycles.has(el)) {
      lifecycles.set(el, { renders: 0, lastRenderAt: 0, renderedSinceLoad: false, changedAt: 0, settledAt: 0, error: null });
    }
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
  document.addEventListener("error", (e) => {
    const level = e instanceof CustomEvent && e.detail ? e.detail.level : null;
    if (level === null) return;  // not an SDK event (e.g. a failed <img>)
    if (level === undefined || level >= 3) lifecycle(e.target).error = { at: performance.now(), detail: e.detail };  // TraceType.Error+
  }, true);

  // `models` (powerbi-models enums) is bundled with the SDK, which loads after this script.
  const sdkModels = () => window["powerbi-client"].models;
  const exportType = (name) => {
    const type = sdkModels().ExportDataType[name];
    if (type === undefined) throw new Error(`Unknown export type: ${name}`);
    return type;
  };

  function configure({ wrapper, budgetMs, settleMs }) {
    state.wrapperSel = wrapper;
    state.deadline = performance.now() + budgetMs;
    state.settleMs = settleMs;
  }

  async function invoke(cfg, fn, args) {
    configure(cfg);
    let timer;
    const expired = new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error(`${fn} timed out after ${cfg.budgetMs}ms`)), cfg.budgetMs);
    });
    try {
      return await Promise.race([window.pbiTool[fn](...args), expired]);
    } finally { clearTimeout(timer); }
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

  // Polls check() until it returns something truthy or the call's budget runs out; check may throw while not ready yet.
  async function waitFor(check, what) {
    const start = performance.now();
    let lastErr;
    while (performance.now() < state.deadline) {
      try {
        const result = check();
        if (result) return result;
      } catch (e) { lastErr = e; }
      await sleep(250);
    }
    const waited = Math.round(performance.now() - start);
    throw new Error(`${what} not ready after ${waited}ms${lastErr ? `: ${lastErr.message}` : ""}`);
  }

  const quiet = (s) => performance.now() - s.lastRenderAt >= state.settleMs;

  async function waitReady() {
    await waitFor(getReport, "Report embed (powerbi.get)");
    const s = await waitFor(() => {
      const s = lifecycle(wrapperEl());
      return s.renderedSinceLoad && quiet(s) && s;
    }, "Report render");
    return { renders: s.renders };
  }

  const markChanged = () => { lifecycle(wrapperEl()).changedAt = performance.now(); };

  // Resolves at once when nothing changed since the last settle; otherwise once a render has followed the last change
  // and settleMs have passed without another. There is no fallback: a change that never renders runs out the budget.
  // A report error fails this wait once; the change stays pending, so a retry keeps waiting for its render.
  async function waitSettled() {
    const s = lifecycle(wrapperEl());
    if (s.changedAt <= s.settledAt) return { rendered: false };
    const outcome = await waitFor(() => {
      if (s.lastRenderAt > s.changedAt && quiet(s)) return "rendered";
      return s.error && s.error.at > s.changedAt && "error";
    }, "Render after the last change");
    if (outcome === "error") {
      const { detail } = s.error;
      s.error = null;
      throw new Error(`Report error: ${JSON.stringify(detail)}`);
    }
    s.settledAt = performance.now();
    return { rendered: true };
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
  const ADVANCED_FILTER = 0;
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

  // A slicer with nothing selected may still report an empty Basic/Advanced filter; that is no selection
  // (filterType null, values []), since filterType describes the current filter, not the slicer.
  const selectsNothing = (f) =>
    (f.filterType === BASIC_FILTER && !(f.values || []).length) ||
    (f.filterType === ADVANCED_FILTER && !(f.conditions || []).length);

  function summariseSlicerState(state) {
    const filter = (state.filters || []).find((f) => !selectsNothing(f));
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
    await page.setActive();
    markChanged();
    await waitSettled();
    return page;
  }

  async function setPage(pageRef) {
    return { page: (await activate(pageRef)).displayName };
  }

  // Show the visuals in keep (matched by name or title) and hide every other visual on the page.
  // This SDK has no setVisualDisplayState; visibility goes through a custom page layout.
  // Hiding needs no data, but showing a visual we hid earlier makes it query, so that counts as a change.
  async function hideVisuals(pageRef, keep) {
    const report = getReport();
    const page = await findPage(report, pageRef);
    const { VisualContainerDisplayMode: Mode, LayoutType } = sdkModels();
    const visualsLayout = {};
    const hidden = [];
    const shownBefore = shownByPage[page.name];
    const shown = new Set();
    for (const v of await page.getVisuals()) {
      const show = keep.includes(v.name) || keep.includes(v.title);
      visualsLayout[v.name] = { displayState: { mode: show ? Mode.Visible : Mode.Hidden } };
      if (show) shown.add(v.name);
      else hidden.push(v.title || v.name);
    }
    delete shownByPage[page.name];  // unknown until the update is accepted
    await report.updateSettings({
      layoutType: LayoutType.Custom,
      customLayout: { pagesLayout: { [page.name]: { visualsLayout } } },
    });
    shownByPage[page.name] = shown;
    const unhidden = shownBefore ? [...shown].filter((n) => !shownBefore.has(n)) : [];
    if (unhidden.length) markChanged();
    return { hidden, unhidden };
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
      markChanged();
    }
    return { scopes: scopes.length };
  }

  // Sets slicer states one at a time (the report is slow). An empty "in" list resets the slicer.
  // The page must already be active.
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
        markChanged();
        const { filterType, values } = summariseSlicerState(await visual.getSlicerState());
        applied.push({ visual: item.visual, ok: true, values, filterType });
      } catch (e) {
        applied.push({ visual: item.visual, ok: false, error: String(e.message || e), values: null, filterType: null });
      }
    }
    return { applied };
  }

  // ---- export ----------------------------------------------------------

  // Waits for pending changes to render (exporting earlier returns the previous data), then reads the visual's data.
  // A read only counts if no render happened during it; with stableReads > 1, consecutive reads must also agree.
  async function exportVisual(pageRef, visualRef, opts = {}) {
    const { type = "Summarized", stableReads = 1 } = opts;
    const report = getReport();
    const page = await findPage(report, pageRef);
    const visual = await findVisual(page, visualRef);
    const s = lifecycle(wrapperEl());
    const settled = await waitSettled();

    let prev = null, agree = 0, reads = 0;
    while (performance.now() < state.deadline) {
      const rendersBefore = s.renders;
      const res = await visual.exportData(exportType(type));
      reads += 1;
      const data = res && res.data;
      if (typeof data !== "string") throw new Error("exportData returned no CSV text");
      if (s.renders !== rendersBefore) {  // redrawn mid-read: start over once it is quiet again
        prev = null, agree = 0;
        await waitFor(() => quiet(s), "Quiet after an unexpected render");
        continue;
      }
      agree = data === prev ? agree + 1 : 1;
      prev = data;
      if (agree >= stableReads) return { csv: data, reads, rendered: settled.rendered };
      await sleep(state.settleMs);
    }
    throw new Error(`Export never stabilised (${reads} reads) - refusing to return possibly stale data`);
  }

  window.pbiTool = {
    invoke, configure, getReport, waitReady, probe, listVisuals, listSlicers, applySlicers, setPage, hideVisuals, applyScopes,
    exportVisual,
  };
})();
