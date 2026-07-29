const graph = window.REPOBRAIN_GRAPH || { nodes: [], edges: [] };
const canvas = document.querySelector("#graph-canvas");
const context = canvas.getContext("2d");
const searchInput = document.querySelector("#graph-search");
const nodeFilter = document.querySelector("#node-filter");
const edgeFilter = document.querySelector("#edge-filter");

const palette = {
  file: "#60a77b", code: "#e2bf56", doc: "#78a5cb",
  test: "#b390cb", env: "#d97861", other: "#9aa69c",
};

function category(type) {
  if (["File", "Directory", "Repository", "Package"].includes(type)) return "file";
  if (["MarkdownDocument", "MarkdownSection", "Decision", "Task", "Concept"].includes(type)) return "doc";
  if (["TestFile", "TestCase"].includes(type)) return "test";
  if (["EnvVar", "ConfigFile", "ConfigKey"].includes(type)) return "env";
  if (["Module", "Function", "Method", "Class", "Variable", "Type", "Interface"].includes(type)) return "code";
  return "other";
}

const hash = (value) => [...value].reduce((total, char) => ((total * 31) + char.charCodeAt(0)) >>> 0, 7);
const nodeTypes = [...new Set(graph.nodes.map((node) => node.type))].sort();
const edgeTypes = [...new Set(graph.edges.map((edge) => edge.type))].sort();
nodeTypes.forEach((type) => nodeFilter.add(new Option(type, type)));
edgeTypes.forEach((type) => edgeFilter.add(new Option(type, type)));

const groups = new Map(nodeTypes.map((type) => [type, []]));
graph.nodes.forEach((node) => groups.get(node.type).push(node));
const ring = 540;
nodeTypes.forEach((type, groupIndex) => {
  const angle = (Math.PI * 2 * groupIndex / nodeTypes.length) - Math.PI / 2;
  const centerX = Math.cos(angle) * ring;
  const centerY = Math.sin(angle) * ring;
  groups.get(type).forEach((node, index) => {
    const seed = hash(node.id);
    const theta = index * 2.399 + (seed % 100) / 100;
    const radius = 18 * Math.sqrt(index);
    node.x = centerX + Math.cos(theta) * radius;
    node.y = centerY + Math.sin(theta) * radius;
    node.color = palette[category(node.type)];
  });
});

const nodeById = new Map(graph.nodes.map((node) => [node.id, node]));
const connections = new Map(graph.nodes.map((node) => [node.id, []]));
graph.edges.forEach((edge) => {
  if (connections.has(edge.source)) connections.get(edge.source).push({ edge, direction: "out", other: nodeById.get(edge.target) });
  if (connections.has(edge.target)) connections.get(edge.target).push({ edge, direction: "in", other: nodeById.get(edge.source) });
});

let view = { x: 0, y: 0, scale: 0.48 };
let selected = null;
let visibleNodes = graph.nodes;
let visibleEdges = graph.edges;
let dragging = false;
let dragStart = null;

function resize() {
  const rect = canvas.getBoundingClientRect();
  const ratio = window.devicePixelRatio || 1;
  canvas.width = Math.round(rect.width * ratio);
  canvas.height = Math.round(rect.height * ratio);
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
  draw();
}

function screen(node) {
  return {
    x: canvas.clientWidth / 2 + view.x + node.x * view.scale,
    y: canvas.clientHeight / 2 + view.y + node.y * view.scale,
  };
}

function draw() {
  context.clearRect(0, 0, canvas.clientWidth, canvas.clientHeight);
  const visibleIds = new Set(visibleNodes.map((node) => node.id));
  context.lineWidth = 0.65;
  visibleEdges.forEach((edge) => {
    if (!visibleIds.has(edge.source) || !visibleIds.has(edge.target)) return;
    const source = screen(nodeById.get(edge.source));
    const target = screen(nodeById.get(edge.target));
    context.strokeStyle = selected && (edge.source === selected.id || edge.target === selected.id)
      ? "rgba(230,197,88,.72)" : "rgba(174,190,178,.10)";
    context.beginPath(); context.moveTo(source.x, source.y); context.lineTo(target.x, target.y); context.stroke();
  });

  visibleNodes.forEach((node) => {
    const point = screen(node);
    const isSelected = selected?.id === node.id;
    const matchesSearch = searchInput.value.trim() && matches(node, searchInput.value);
    context.beginPath();
    context.arc(point.x, point.y, isSelected ? 7 : matchesSearch ? 5 : Math.max(1.8, 3.2 * view.scale), 0, Math.PI * 2);
    context.fillStyle = node.color;
    context.globalAlpha = selected && !isSelected && !connections.get(selected.id).some((item) => item.other?.id === node.id) ? 0.22 : 0.92;
    context.fill(); context.globalAlpha = 1;
    if (isSelected) { context.strokeStyle = "#fff8d6"; context.lineWidth = 2; context.stroke(); }
  });
  document.querySelector("#graph-empty").hidden = visibleNodes.length > 0;
}

function matches(node, query) {
  const value = query.trim().toLowerCase();
  return !value || [node.name, node.qualified_name, node.path, node.type].some((field) => (field || "").toLowerCase().includes(value));
}

function applyFilters() {
  visibleNodes = graph.nodes.filter((node) => (nodeFilter.value === "all" || node.type === nodeFilter.value) && matches(node, searchInput.value));
  visibleEdges = graph.edges.filter((edge) => edgeFilter.value === "all" || edge.type === edgeFilter.value);
  document.querySelector("#visible-count").textContent = visibleNodes.length.toLocaleString();
  if (selected && !visibleNodes.some((node) => node.id === selected.id)) selectNode(null);
  draw();
}

function nearestNode(clientX, clientY) {
  const rect = canvas.getBoundingClientRect();
  const x = clientX - rect.left;
  const y = clientY - rect.top;
  let nearest = null;
  let distance = 12;
  visibleNodes.forEach((node) => {
    const point = screen(node);
    const current = Math.hypot(point.x - x, point.y - y);
    if (current < distance) { nearest = node; distance = current; }
  });
  return nearest;
}

function selectNode(node) {
  selected = node;
  document.querySelector("#inspector-empty").hidden = Boolean(node);
  document.querySelector("#inspector-content").hidden = !node;
  if (!node) { draw(); return; }
  document.querySelector("#node-swatch").style.background = node.color;
  document.querySelector("#node-type").textContent = node.type;
  document.querySelector("#node-name").textContent = node.name;
  document.querySelector("#node-qualified").textContent = node.qualified_name || node.path;
  const lines = node.start_line ? `:${node.start_line}${node.end_line && node.end_line !== node.start_line ? `–${node.end_line}` : ""}` : "";
  document.querySelector("#node-source").textContent = `${node.path || "repository-wide"}${lines}`;
  document.querySelector("#node-language").textContent = node.language || "—";
  document.querySelector("#node-confidence").textContent = `${Math.round((node.confidence || 0) * 100)}%`;
  const related = connections.get(node.id) || [];
  document.querySelector("#node-connections").textContent = related.length;
  const list = document.querySelector("#relation-list");
  list.replaceChildren();
  related.slice(0, 20).forEach(({ edge, direction, other }) => {
    const item = document.createElement("article");
    const relation = document.createElement("b");
    relation.textContent = `${direction === "out" ? "→" : "←"} ${edge.type}`;
    const name = document.createElement("span"); name.textContent = other?.name || "Unknown node";
    const source = document.createElement("small"); source.textContent = edge.path + (edge.start_line ? `:${edge.start_line}` : "");
    item.append(relation, name, source); list.append(item);
  });
  draw();
}

canvas.addEventListener("pointerdown", (event) => { dragging = true; dragStart = { x: event.clientX, y: event.clientY, viewX: view.x, viewY: view.y }; canvas.classList.add("dragging"); canvas.setPointerCapture(event.pointerId); });
canvas.addEventListener("pointermove", (event) => { if (!dragging) return; view.x = dragStart.viewX + event.clientX - dragStart.x; view.y = dragStart.viewY + event.clientY - dragStart.y; draw(); });
canvas.addEventListener("pointerup", (event) => { const moved = Math.hypot(event.clientX - dragStart.x, event.clientY - dragStart.y); dragging = false; canvas.classList.remove("dragging"); if (moved < 5) selectNode(nearestNode(event.clientX, event.clientY)); });
canvas.addEventListener("wheel", (event) => { event.preventDefault(); view.scale = Math.max(.18, Math.min(2.5, view.scale * Math.exp(-event.deltaY * .001))); draw(); }, { passive: false });
searchInput.addEventListener("input", applyFilters); nodeFilter.addEventListener("change", applyFilters); edgeFilter.addEventListener("change", applyFilters);
document.querySelector("#reset-view").addEventListener("click", () => { view = { x: 0, y: 0, scale: .48 }; searchInput.value = ""; nodeFilter.value = "all"; edgeFilter.value = "all"; selectNode(null); applyFilters(); });

// Provenance is read from the snapshot itself so the label can never drift
// from the data it describes.
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
function snapshotDate() {
  const parts = /^(\d{4})-(\d{2})-(\d{2})$/.exec(graph.generated_at || "");
  return parts ? `${MONTHS[Number(parts[2]) - 1]} ${Number(parts[3])}, ${parts[1]}` : null;
}
const provenance = [snapshotDate(), graph.commit].filter(Boolean);
document.querySelector("[data-snapshot-label]").textContent = ["Snapshot", ...provenance].join(" · ");
document.querySelector("[data-snapshot-footer]").textContent = provenance.length
  ? `Self-index snapshot generated ${provenance.join(" at ")}.`
  : "Self-index snapshot of this repository.";

document.querySelector("#node-count").textContent = graph.nodes.length.toLocaleString();
document.querySelector("#edge-count").textContent = graph.edges.length.toLocaleString();
document.querySelector("#type-count").textContent = nodeTypes.length;
document.querySelector("#visible-count").textContent = graph.nodes.length.toLocaleString();
window.addEventListener("resize", resize);
resize();
