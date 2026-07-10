const state = {
  platform: localStorage.getItem("repobrain-platform") || "unix",
  completed: new Set(JSON.parse(localStorage.getItem("repobrain-steps") || "[]")),
};

const tabs = document.querySelectorAll(".platform-tab");
const commands = document.querySelectorAll(".command");
const navLinks = document.querySelectorAll(".step-link");
const cards = document.querySelectorAll(".step-card");
const toast = document.querySelector(".toast");

function renderPlatform() {
  tabs.forEach((tab) => {
    const active = tab.dataset.platform === state.platform;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-selected", String(active));
  });

  commands.forEach((block) => {
    const value = block.dataset[`command${state.platform[0].toUpperCase()}${state.platform.slice(1)}`];
    block.querySelector(".command-text").textContent = value;
  });
}

function renderProgress() {
  navLinks.forEach((link) => link.classList.toggle("done", state.completed.has(Number(link.dataset.step))));
  const progress = Math.max(0, Math.min(100, (state.completed.size / 5) * 100));
  document.querySelector("#progress-line").style.height = `${progress}%`;
  document.querySelectorAll(".complete-btn").forEach((button) => {
    const done = state.completed.has(Number(button.closest(".step-card").dataset.step));
    button.classList.toggle("completed", done);
    button.innerHTML = done ? "<span>✓</span> Completed" : `<span>✓</span> ${button.closest(".final-card") ? "Finish setup" : "Mark complete"}`;
  });
  localStorage.setItem("repobrain-steps", JSON.stringify([...state.completed]));
}

tabs.forEach((tab) => tab.addEventListener("click", () => {
  state.platform = tab.dataset.platform;
  localStorage.setItem("repobrain-platform", state.platform);
  renderPlatform();
}));

document.querySelectorAll(".copy-btn").forEach((button) => button.addEventListener("click", async () => {
  const text = button.closest(".command").querySelector(".command-text").textContent;
  await navigator.clipboard.writeText(text);
  button.textContent = "Copied ✓";
  toast.classList.add("show");
  setTimeout(() => { button.textContent = "Copy"; toast.classList.remove("show"); }, 1600);
}));

document.querySelectorAll(".complete-btn").forEach((button) => button.addEventListener("click", () => {
  const step = Number(button.closest(".step-card").dataset.step);
  state.completed.has(step) ? state.completed.delete(step) : state.completed.add(step);
  renderProgress();
}));

navLinks.forEach((link) => link.addEventListener("click", () => {
  document.querySelector(`#step-${link.dataset.step}`).scrollIntoView({ behavior: "smooth", block: "start" });
}));

document.querySelector("#reset-progress").addEventListener("click", () => { state.completed.clear(); renderProgress(); });

const observer = new IntersectionObserver((entries) => {
  const visible = entries.filter((entry) => entry.isIntersecting).sort((a, b) => b.intersectionRatio - a.intersectionRatio)[0];
  if (!visible) return;
  const step = visible.target.dataset.step;
  cards.forEach((card) => card.classList.toggle("is-current", card.dataset.step === step));
  navLinks.forEach((link) => link.classList.toggle("active", link.dataset.step === step));
}, { rootMargin: "-20% 0px -55%", threshold: [0.1, 0.4, 0.7] });

cards.forEach((card) => observer.observe(card));
renderPlatform();
renderProgress();
