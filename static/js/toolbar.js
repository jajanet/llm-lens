// Persistent toolbar above the content area.
// The search input is kept in the DOM between renders so it keeps focus while typing.

import { state } from "./state.js";

const toolbarArea = document.getElementById("toolbar-area");
const searchBox = document.getElementById("search-box");
const toolbarExtra = document.getElementById("toolbar-extra");

let searchHandler = null;

searchBox.addEventListener("input", (e) => {
  if (searchHandler) searchHandler(e.target.value);
});

export function configureToolbar({ placeholder, extraHtml, onSearch, searchValue }) {
  toolbarArea.style.display = "block";
  searchBox.placeholder = placeholder || "Filter...";
  if (searchValue !== undefined && searchBox.value !== searchValue) {
    searchBox.value = searchValue;
  }
  toolbarExtra.innerHTML = extraHtml || "";
  searchHandler = onSearch || null;
}

export function hideToolbar() {
  toolbarArea.style.display = "none";
  searchHandler = null;
}

export function updateEditButton() {
  const btn = document.getElementById("edit-toggle");
  btn.style.display = state.view === "messages" ? "" : "none";
  if (state.view !== "messages" && state.editMode) {
    state.editMode = false;
    document.body.classList.remove("edit-mode");
    btn.textContent = "Edit";
    btn.classList.remove("active");
  }
}
