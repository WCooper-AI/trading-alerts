/* Trading Alerts: a strategy's saved versions. See what changed between them and restore an old one. */

"use strict";

/** Line-by-line differences between two texts: [{ kind: "same" | "add" | "del", text }]. */
function lineDiff(a, b) {
  const x = a.split("\n"), y = b.split("\n");
  const n = x.length, m = y.length;
  // longest common subsequence table, from the end
  const lcs = Array.from({ length: n + 1 }, () => new Uint16Array(m + 1));
  for (let i = n - 1; i >= 0; i--)
    for (let j = m - 1; j >= 0; j--)
      lcs[i][j] = x[i] === y[j] ? lcs[i + 1][j + 1] + 1 : Math.max(lcs[i + 1][j], lcs[i][j + 1]);
  const out = [];
  let i = 0, j = 0;
  while (i < n && j < m) {
    if (x[i] === y[j]) { out.push({ kind: "same", text: x[i] }); i++; j++; }
    else if (lcs[i + 1][j] >= lcs[i][j + 1]) out.push({ kind: "del", text: x[i++] });
    else out.push({ kind: "add", text: y[j++] });
  }
  while (i < n) out.push({ kind: "del", text: x[i++] });
  while (j < m) out.push({ kind: "add", text: y[j++] });
  return out;
}

/** The differences, with unchanged stretches folded down to a few lines of context. */
function renderDiff(rows, context = 3) {
  const changed = rows.map((r) => r.kind !== "same");
  const near = rows.map((_, i) => changed.slice(Math.max(0, i - context), i + context + 1).some(Boolean));
  const list = h("div", { class: "diff", role: "table", "aria-label": "Changes" });
  let hidden = 0;
  const flush = () => {
    if (hidden) list.appendChild(h("div", { class: "diff-gap" }, `${hidden} unchanged line${hidden === 1 ? "" : "s"}`));
    hidden = 0;
  };
  rows.forEach((r, i) => {
    if (!near[i]) { hidden++; return; }
    flush();
    const sign = r.kind === "add" ? "+" : r.kind === "del" ? "−" : " ";
    const label = r.kind === "add" ? "Added: " : r.kind === "del" ? "Removed: " : "";
    list.appendChild(h("div", { class: "diff-line " + r.kind, role: "row" },
      h("span", { class: "diff-sign", "aria-hidden": "true" }, sign),
      h("code", {}, label ? h("span", { class: "sr-only" }, label) : null, r.text || " ")));
  });
  flush();
  if (!changed.some(Boolean)) return h("p", { class: "muted" }, "No differences.");
  return list;
}

function versionSentence(data) {
  try {
    const d = clone(data);
    d.entry = asGroup(d.entry);
    d.exit = d.exit || {};
    if (d.exit.when) d.exit.when = asGroup(d.exit.when);
    return renderParts(describeStrategy(d));
  } catch (_) { return null; }
}

async function viewHistory(id) {
  setNav("strategies");
  const [rec, list] = await Promise.all([GET("/strategies/" + encodeURIComponent(id)),
    GET(`/strategies/${encodeURIComponent(id)}/versions`)]);
  const versions = list.versions;
  const current = list.current;
  const runVersion = rec.run ? rec.run.version : null;
  const texts = {};
  const load = async (no) => (texts[no] = texts[no] || (await GET(`/strategies/${encodeURIComponent(id)}/versions/${no}`)));

  const detail = h("div", { class: "panel history-detail", "aria-live": "polite" });
  const items = h("ol", { class: "version-list" });
  let selected = current;
  let against = "previous";

  async function show(no) {
    selected = no;
    for (const li of items.children) li.classList.toggle("selected", Number(li.dataset.no) === no);
    const v = await load(no);
    const older = versions.find((x) => x.version_no < no);
    const base = against === "current" && no !== current ? await load(current) : older ? await load(older.version_no) : null;
    const compare = h("div", { class: "segmented", role: "group", "aria-label": "Compare with" },
      [["previous", older ? `Changes from version ${older.version_no}` : "First kept version"], ["current", "Compared with now"]]
        .filter(([k]) => k === "previous" || no !== current)
        .map(([k, label]) => h("button", { type: "button", "aria-pressed": String(against === k),
          onclick: () => { against = k; show(no); } }, label)));
    let diff;
    if (!base) diff = h("p", { class: "muted" }, "This is the earliest version kept, so there's nothing to compare it with.");
    else if (against === "current" && no !== current) diff = renderDiff(lineDiff(v.yaml, base.yaml));
    else diff = renderDiff(lineDiff(base.yaml, v.yaml));
    const restore = no === current ? null : h("button", { type: "button", class: "primary", onclick: async () => {
      const r = await attempt(() => withRunningChoice((onRunning) =>
        POST(`/strategies/${encodeURIComponent(id)}/versions/${no}/restore`, { on_running: onRunning }),
        { message: `This strategy has a forward test running. Restoring version ${no} changes its trading rules.`, cancel: "Don't restore" }));
      if (!r) return;
      toast(r.forward_test === "restarted" ? `Version ${no} restored. A fresh forward test has started`
        : `Version ${no} restored as version ${r.version}`);
      refreshStatus();
      viewHistory(id);
    } }, `Restore version ${no}`);
    clear(detail);
    append(detail, [h("section", { class: "section" },
      h("div", { class: "row-between" },
        h("div", {}, h("h2", {}, `Version ${no}`), h("p", { class: "small muted" }, `${v.note || "Saved"}, ${when(v.saved_at)}`)),
        restore),
      versionSentence(v.data),
      compare,
      against === "current" && no !== current ? h("p", { class: "small muted" }, `Lines marked − are in version ${no}; + are how the strategy is now.`) : null,
      diff)]);
  }

  for (const v of versions) {
    const badges = [];
    if (v.version_no === current) badges.push(h("span", { class: "tag" }, "Current"));
    if (v.version_no === runVersion) badges.push(h("span", { class: "tag", title: "The running forward test uses these rules" }, "Forward test"));
    items.appendChild(h("li", { "data-no": v.version_no },
      h("button", { type: "button", class: "version-item", onclick: () => show(v.version_no) },
        h("span", { class: "version-no" }, `Version ${v.version_no}`), ...badges,
        h("span", { class: "small muted" }, `${v.note || "Saved"}, ${when(v.saved_at)}`))));
  }

  const note = list.kept ? ` The newest ${list.kept} are kept.` : "";
  clear($main);
  append($main, [
    h("div", { class: "editor-head" },
      h("a", { class: "back", href: "#/s/" + encodeURIComponent(id) }, rec.data.name || id),
      h("h1", {}, "History"),
      h("p", { class: "muted" }, `A version is kept each time this strategy is saved with different settings.${note} Restoring an old one saves it as a new version, so nothing is lost.`)),
    versions.length
      ? h("div", { class: "history" }, h("div", { class: "panel" }, h("section", { class: "section" }, items)), detail)
      : h("div", { class: "panel" }, h("div", { class: "empty" }, h("p", {}, "No versions kept yet. One is kept each time the strategy is saved."))),
  ]);
  if (versions.length) await show(selected);
}
