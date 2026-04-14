// Confirmation modal helper.

export function showConfirmModal({ title, body, onConfirm, confirmLabel = "Delete" }) {
  const overlay = document.createElement("div");
  overlay.className = "modal-overlay";
  overlay.innerHTML = `
    <div class="modal">
      <h3>${title}</h3>
      <p>${body}</p>
      <div class="modal-actions">
        <button class="btn-cancel" data-modal-cancel>Cancel</button>
        <button class="btn-confirm-delete" data-modal-confirm>${confirmLabel}</button>
      </div>
    </div>`;
  document.body.appendChild(overlay);

  function close() { overlay.remove(); }

  overlay.addEventListener("click", (e) => {
    if (e.target === overlay || e.target.matches("[data-modal-cancel]")) close();
    else if (e.target.matches("[data-modal-confirm]")) {
      close();
      onConfirm();
    }
  });
}


// Read-only info modal (no confirm action). Body may be HTML.
export function showInfoModal({ title, body }) {
  const overlay = document.createElement("div");
  overlay.className = "modal-overlay";
  overlay.innerHTML = `
    <div class="modal">
      <h3>${title}</h3>
      <div class="modal-body">${body}</div>
      <div class="modal-actions">
        <button class="btn-cancel" data-modal-cancel>Close</button>
      </div>
    </div>`;
  document.body.appendChild(overlay);
  overlay.addEventListener("click", (e) => {
    if (e.target === overlay || e.target.matches("[data-modal-cancel]")) overlay.remove();
  });
}
