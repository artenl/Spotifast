// Small behaviours shared by every page (no inline JS: the CSP forbids it).
"use strict";

document.addEventListener("submit", (event) => {
  const form = event.target;
  const question = form.getAttribute && form.getAttribute("data-confirm");
  if (question && !window.confirm(question)) event.preventDefault();
});

document.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-copy]");
  if (!button) return;
  const source = document.getElementById(button.getAttribute("data-copy"));
  if (!source) return;
  try {
    await navigator.clipboard.writeText(source.textContent.trim());
    const label = button.textContent;
    button.textContent = "Copié ✓";
    setTimeout(() => { button.textContent = label; }, 1600);
  } catch {
    window.getSelection().selectAllChildren(source);
  }
});
