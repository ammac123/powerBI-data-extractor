// Injected into the host page. Exposes window.pbiTool, a thin async API over the
// powerbi-client SDK. Python calls these via page.evaluate and passes plain JSON.
// Written defensively: the host site may run an old SDK, so every optional API is
// feature-detected rather than assumed.
//
// window.pbiTool API (pages/visuals are matched by internal name or title; duplicate titles throw):
//   getReport(wrapperSel?)            -> SDK report for the embed wrapper
//   waitReady(wrapperSel, timeoutMs)  -> { pages } once the report lists pages
//   probe(wrapperSel)                 -> which SDK APIs are available
//   listVisuals(pageRef?)             -> [{ page, pageName, name, title, type }]
//   setPage(pageRef)                  -> activates a page
//   hideVisuals(pageRef, keep)        -> hides every visual on the page except keep
//   applyScopes(scopes)               -> sets (replaces) filters per report/page/visual scope
//   exportVisual(pageRef, visualRef, opts) -> { csv, reads, rendered }; page must already be active
//   renderCount()                     -> renders seen so far (pass as opts.sinceCount)
(() => {
  if (window.pbiTool) return;

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const state = { report: null, wrapperSel: null, renderCount: 0, lastRenderAt: 0, listening: false };

  // SDK enum fallbacks (numeric values from powerbi-models) for when `models` is missing.
  const sdkModels = () => (window["powerbi-client"] && window["powerbi-client"].models) || {};
  const HIDDEN = () => ((sdkModels().VisualContainerDisplayMode || {}).Hidden ?? 1);
  const VISIBLE = () => ((sdkModels().VisualContainerDisplayMode || {}).Visible ?? 0);
  const CUSTOM_LAYOUT = () => ((sdkModels().LayoutType || {}).Custom ?? 1);
  const exportType = (name) => {
    const t = sdkModels().ExportDataType || {};
    return t[name] ?? (name === "Underlying" ? 1 : 0);
  };

  function getReport(wrapperSel) {
    state.wrapperSel = wrapperSel || state.wrapperSel;
    const el = document.querySelector(state.wrapperSel);
    if (!el) throw new Error(`Embed wrapper not found: ${state.wrapperSel}`);
    if (!window.powerbi) throw new Error("window.powerbi is not defined");
    const report = window.powerbi.get(el);
    if (!report) throw new Error("powerbi.get() returned nothing for the wrapper");
    if (report !== state.report) {
      state.report = report;
      state.listening = false;
    }
    if (!state.listening && typeof report.on === "function") {
      // Counted for freshness checks in exportVisual().
      const bump = () => { state.renderCount += 1; state.lastRenderAt = performance.now(); };
      report.on("rendered", bump);
      report.on("loaded", bump);
      state.listening = true;
    }
    return report;
  }

  async function waitReady(wrapperSel, timeoutMs = 60000) {
    const deadline = performance.now() + timeoutMs;
    let lastErr;
    while (performance.now() < deadline) {
      try {
        const report = getReport(wrapperSel);
        const pages = await report.getPages();
        if (pages && pages.length) return { pages: pages.length };
      } catch (e) { lastErr = e; }
      await sleep(500);
    }
    throw new Error(`Report not ready after ${timeoutMs}ms: ${lastErr ? lastErr.message : "no pages"}`);
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

  async function probe(wrapperSel) {
    const caps = { sdk: {}, report: {}, page: {}, visual: {}, events: {} };
    const ns = window["powerbi-client"];
    caps.sdk.namespaceKeys = ns ? Object.keys(ns) : null;
    caps.sdk.version = (ns && (ns.version || ns.VERSION)) || null;
    caps.sdk.serviceClass = window.powerbi ? window.powerbi.constructor.name : null;
    caps.sdk.modelsEnums = ["ExportDataType", "VisualContainerDisplayMode", "LayoutType", "FilterType"]
      .reduce((o, k) => ((o[k] = !!(sdkModels()[k])), o), {});
    caps.sdk.scriptSrcs = Array.from(document.scripts).map((s) => s.src).filter(Boolean);

    const report = getReport(wrapperSel);
    const has = (o, k) => typeof o[k] === "function";
    for (const k of ["getPages", "getFilters", "setFilters", "updateSettings", "setVisualDisplayState", "on", "off", "getActivePage"]) {
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
    return caps;
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

  async function setPage(pageRef) {
    const report = getReport();
    const page = await findPage(report, pageRef);
    await page.setActive();
    return { page: page.displayName };
  }

  // Show the visuals in keep (matched by name or title) and hide every other visual on the page.
  async function hideVisuals(pageRef, keep) {
    const report = getReport();
    if (typeof report.setVisualDisplayState !== "function") {
      return { skipped: "setVisualDisplayState unsupported by this SDK version" };
    }
    const page = await findPage(report, pageRef);
    const visuals = await page.getVisuals();
    // Custom layout is required before display state can be changed.
    await report.updateSettings({ layoutType: CUSTOM_LAYOUT() });
    const hidden = [];
    for (const v of visuals) {
      if (keep.includes(v.name) || keep.includes(v.title)) {
        await report.setVisualDisplayState(page.name, v.name, VISIBLE());
        continue;
      }
      await report.setVisualDisplayState(page.name, v.name, HIDDEN());
      hidden.push(v.title || v.name);
    }
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

  // ---- freshness + export ------------------------------------------------

  // Resolves once at least one render has happened since `sinceCount`, and then the report has been
  // quiet for settleMs. If the SDK never emits (or nothing re-renders because the filter was a no-op),
  // gives up waiting for the first render after firstRenderMs and relies on the data-stability check.
  async function waitSettled({ sinceCount, settleMs, firstRenderMs, timeoutMs }) {
    const start = performance.now();
    while (performance.now() - start < timeoutMs) {
      const rendered = state.renderCount > sinceCount;
      const quiet = performance.now() - state.lastRenderAt >= settleMs;
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
      timeoutMs = 60000, stableReads = 2, pollMs = 500, sinceCount = state.renderCount,
    } = opts;
    const report = getReport();
    const page = await findPage(report, pageRef);
    const visual = await findVisual(page, visualRef);
    if (typeof visual.exportData !== "function") throw new Error("visual.exportData is not supported by this SDK version");

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
    getReport, waitReady, probe, listVisuals, setPage, hideVisuals, applyScopes, exportVisual,
    renderCount: () => state.renderCount,
  };
})();
