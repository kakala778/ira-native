const shell = document.querySelector(".app-shell");
const navToggle = document.querySelector("[data-nav-toggle]");

function setNavigation(collapsed) {
  shell.classList.toggle("nav-collapsed", collapsed);
  navToggle.setAttribute("aria-expanded", String(!collapsed));
  navToggle.setAttribute("aria-label", collapsed ? "展开导航" : "折叠导航");
}

setNavigation(sessionStorage.getItem("ira.nav.collapsed") === "true");
navToggle.addEventListener("click", () => {
  const collapsed = navToggle.getAttribute("aria-expanded") === "true";
  setNavigation(collapsed);
  sessionStorage.setItem("ira.nav.collapsed", String(collapsed));
});

const evidence = document.querySelector("[data-evidence-panel]");
const trigger = document.querySelector("[data-evidence-toggle]");
const narrow = matchMedia("(max-width: 1100px)");
const workspace = document.querySelector(".workspace");
const nav = document.querySelector(".app-nav");

function syncDrawer() {
  const modal = narrow.matches && !evidence.hidden;
  workspace.inert = nav.inert = modal;
  document.querySelector(".skip-link").inert = modal;
  evidence.setAttribute("role", modal ? "dialog" : "complementary");
  if (modal) {
    evidence.setAttribute("aria-modal", "true");
    if (!evidence.contains(document.activeElement)) {
      evidence.querySelector("[data-evidence-close]").focus();
    }
  } else {
    evidence.removeAttribute("aria-modal");
  }
}
function setEvidence(open) {
  evidence.hidden = !open;
  trigger.setAttribute("aria-expanded", String(open));
  syncDrawer();
  (open ? evidence.querySelector("[data-evidence-close]") : trigger).focus();
}
if (evidence && trigger) {
  narrow.addEventListener("change", syncDrawer);
  syncDrawer();
  trigger.addEventListener("click", () => setEvidence(evidence.hidden));
  evidence.querySelector("[data-evidence-close]").addEventListener("click", () => setEvidence(false));
  evidence.addEventListener("keydown", event => {
    if (event.key === "Escape") setEvidence(false);
  });
}

document.querySelector("[data-restore-focus]")?.focus();

for (const row of document.querySelectorAll("[data-task-input]")) {
  const selected = row.querySelector("[data-task-select]");
  const fields = row.querySelectorAll("select, input:not([type=checkbox])");
  function syncSelection() {
    for (const field of fields) {
      field.disabled = !selected.checked;
      field.required = selected.checked;
    }
  }
  selected.addEventListener("change", syncSelection);
  syncSelection();
}

const printDetails = [...document.querySelectorAll("[data-print-open]")];
let expandedForPrint = [];
window.addEventListener("beforeprint", () => {
  expandedForPrint = printDetails.filter(detail => !detail.open);
  for (const detail of expandedForPrint) detail.open = true;
});
window.addEventListener("afterprint", () => {
  for (const detail of expandedForPrint) detail.open = false;
  expandedForPrint = [];
});

for (const group of document.querySelectorAll("[data-local-pagination]")) {
  const rows = [...group.querySelectorAll("[data-list-row]")];
  const filter = group.querySelector("[data-list-filter]");
  const previous = group.querySelector("[data-list-prev]");
  const next = group.querySelector("[data-list-next]");
  const count = group.querySelector("[data-list-count]");
  const size = 8;
  let page = 0;
  function render() {
    const shown = rows.filter(row => !filter.value || row.dataset.listRow === filter.value);
    const end = Math.max(0, Math.ceil(shown.length / size) - 1);
    page = Math.max(0, Math.min(page, end));
    const visible = new Set(shown.slice(page * size, (page + 1) * size));
    for (const row of rows) row.hidden = !visible.has(row);
    previous.disabled = page === 0;
    next.disabled = page === end;
    count.textContent = shown.length + " 项 · " + (page + 1) + " / " + (end + 1);
  }
  filter.addEventListener("change", () => { page = 0; render(); });
  previous.addEventListener("click", () => { page--; render(); });
  next.addEventListener("click", () => { page++; render(); });
  render();
}
