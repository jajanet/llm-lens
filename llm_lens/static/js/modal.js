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
