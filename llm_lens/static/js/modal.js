// Confirmation modal helper.

export function showConfirmModal({ title, body, onConfirm, onDuplicate, confirmLabel = "Delete", duplicateLabel = "Dup &amp; Delete" }) {
  const overlay = document.createElement("div");
  overlay.className = "modal-overlay";
  const dupBtn = onDuplicate
    ? `<button class="btn-cancel" data-modal-duplicate>${duplicateLabel}</button>`
    : "";
  overlay.innerHTML = `
    <div class="modal">
      <h3>${title}</h3>
      <p>${body}</p>
      <div class="modal-actions">
        <button class="btn-cancel" data-modal-cancel>Cancel</button>
        ${dupBtn}
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
    } else if (e.target.matches("[data-modal-duplicate]")) {
      close();
      onDuplicate();
    }
  });
}


// Read-only info modal (no confirm action). Body may be HTML.
export function showInfoModal({ title, body }) {
  const overlay = document.createElement("div");
  overlay.className = "modal-overlay";
  overlay.innerHTML = `
    <div class="modal">
      <button class="modal-close" data-modal-cancel aria-label="Close">&times;</button>
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
