const filterButtons = document.querySelectorAll(".filter-tabs button");
const capabilities = document.querySelectorAll(".capability-list article");

filterButtons.forEach((button) => button.addEventListener("click", () => {
  const filter = button.dataset.filter;
  filterButtons.forEach((candidate) => {
    const active = candidate === button;
    candidate.classList.toggle("active", active);
    candidate.setAttribute("aria-selected", String(active));
  });
  capabilities.forEach((capability) => {
    capability.classList.toggle(
      "hidden",
      filter !== "all" && capability.dataset.status !== filter,
    );
  });
}));
