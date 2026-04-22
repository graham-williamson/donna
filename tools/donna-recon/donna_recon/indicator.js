// donna-recon — page-side indicator and F9 capture handler.
//
// Injected into every document via `context.add_init_script`. Runs before
// page scripts. Two jobs:
//
//   1. Draw a small fixed-position "REC" badge so the recorder's presence
//      is visible in every tab.
//   2. Bind keydown for F9 — show an in-page DOM modal for a label, then
//      call `window.__donnaMark(label)`. A DOM modal (not window.prompt)
//      because Playwright auto-dismisses native dialogs when attached via
//      CDP. The `./donna-recon mark <label>` CLI fallback stays available
//      for pages whose CSP blocks injected DOM.

(() => {
  // Guard against re-injection on same-document navigations.
  if (window.__donnaReconAttached) return;
  window.__donnaReconAttached = true;

  function drawBadge() {
    if (!document.body) {
      document.addEventListener("DOMContentLoaded", drawBadge, { once: true });
      return;
    }
    if (document.getElementById("donna-recon-badge")) return;
    const badge = document.createElement("div");
    badge.id = "donna-recon-badge";
    badge.textContent = "REC ●  donna-recon  •  F9 to mark";
    Object.assign(badge.style, {
      position: "fixed",
      right: "12px",
      bottom: "12px",
      zIndex: "2147483647",
      padding: "6px 10px",
      fontFamily: "-apple-system, BlinkMacSystemFont, sans-serif",
      fontSize: "11px",
      fontWeight: "600",
      color: "#fff",
      background: "rgba(185, 28, 28, 0.9)",
      borderRadius: "4px",
      boxShadow: "0 2px 6px rgba(0,0,0,0.35)",
      pointerEvents: "none",
      userSelect: "none",
    });
    document.body.appendChild(badge);
  }

  function syntheticLabel() {
    const ts = new Date().toISOString().replace(/[:.]/g, "-");
    return `marker-${ts}`;
  }

  let modalOpen = false;

  function showLabelModal() {
    if (modalOpen) return;
    if (!document.body) {
      // No DOM to host the modal; fall back to synthetic label so the
      // moment isn't lost.
      emitMark(syntheticLabel());
      return;
    }
    modalOpen = true;

    const overlay = document.createElement("div");
    Object.assign(overlay.style, {
      position: "fixed",
      inset: "0",
      zIndex: "2147483646",
      background: "rgba(0, 0, 0, 0.45)",
      display: "flex",
      alignItems: "center",
      justifyContent: "center",
      fontFamily: "-apple-system, BlinkMacSystemFont, sans-serif",
    });

    const dialog = document.createElement("div");
    Object.assign(dialog.style, {
      background: "#fff",
      borderRadius: "8px",
      padding: "20px",
      width: "360px",
      boxShadow: "0 8px 24px rgba(0,0,0,0.35)",
      color: "#111",
    });

    const title = document.createElement("div");
    title.textContent = "donna-recon: what state is this?";
    Object.assign(title.style, {
      fontSize: "13px",
      fontWeight: "600",
      marginBottom: "10px",
    });

    const input = document.createElement("input");
    input.type = "text";
    input.placeholder = "e.g. bookable class row";
    Object.assign(input.style, {
      width: "100%",
      padding: "8px 10px",
      fontSize: "13px",
      border: "1px solid #ccc",
      borderRadius: "4px",
      boxSizing: "border-box",
      outline: "none",
    });

    const hint = document.createElement("div");
    hint.textContent = "Enter to save · Esc to cancel";
    Object.assign(hint.style, {
      fontSize: "11px",
      color: "#666",
      marginTop: "8px",
      textAlign: "right",
    });

    dialog.appendChild(title);
    dialog.appendChild(input);
    dialog.appendChild(hint);
    overlay.appendChild(dialog);
    document.body.appendChild(overlay);

    function close() {
      if (!modalOpen) return;
      modalOpen = false;
      if (overlay.parentNode) overlay.parentNode.removeChild(overlay);
    }

    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        const label = input.value.trim() || syntheticLabel();
        close();
        emitMark(label);
      } else if (e.key === "Escape") {
        e.preventDefault();
        close();
      }
    });

    // Focus after the browser settles layout.
    setTimeout(() => input.focus(), 0);
  }

  async function emitMark(label) {
    if (typeof window.__donnaMark === "function") {
      try {
        await window.__donnaMark(label);
      } catch (e) {
        // Python side logs the failure; nothing useful we can do here.
      }
    }
  }

  document.addEventListener(
    "keydown",
    (e) => {
      if (e.key === "F9" || e.code === "F9") {
        e.preventDefault();
        e.stopPropagation();
        showLabelModal();
      }
    },
    true // capture phase — beat page handlers that stopPropagation
  );

  drawBadge();
  // Re-draw on SPA route changes that reset the DOM without reloading.
  const mo = new MutationObserver(() => {
    if (!document.getElementById("donna-recon-badge")) drawBadge();
  });
  if (document.documentElement) {
    mo.observe(document.documentElement, { childList: true, subtree: true });
  }
})();
