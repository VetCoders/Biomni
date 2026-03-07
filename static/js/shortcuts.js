/* ==========================================================================
   Biomni Portal — shortcuts.js
   Global keyboard shortcuts + helper overlay.
   ========================================================================== */

const IS_MAC = /Mac|iPhone|iPad|iPod/i.test(navigator.platform);

function shortcutLabel(base) {
  return IS_MAC ? `Cmd+${base}` : `Ctrl+${base}`;
}

function resolveElement(target) {
  if (!target) return null;
  if (target instanceof HTMLElement) return target;
  if (typeof target === "string") return document.querySelector(target);
  return null;
}

function isModifierPressed(event) {
  return IS_MAC ? event.metaKey : event.ctrlKey;
}

function keyIsSlash(event) {
  return event.key === "/" || event.key === "?" || event.code === "Slash";
}

function focusSidebarSearch() {
  const searchField = document.querySelector(
    "#sidebar-search, #conversation-search, [data-role='sidebar-search'], .sidebar input[type='search']"
  );
  if (searchField instanceof HTMLElement) {
    searchField.focus();
    if ("select" in searchField && typeof searchField.select === "function") {
      searchField.select();
    }
    return true;
  }
  return false;
}

function createOverlay() {
  const rows = [
    ["Focus sidebar search", shortcutLabel("K")],
    ["New conversation", shortcutLabel("N")],
    ["Export conversation", shortcutLabel("Shift+E")],
    ["Show shortcuts", shortcutLabel("/")],
    ["Close modal / overlay", "Esc"],
    ["Send message", "Enter"],
  ];

  const overlay = document.createElement("div");
  overlay.className = "shortcuts-overlay";
  overlay.setAttribute("aria-hidden", "true");

  const panel = document.createElement("div");
  panel.className = "shortcuts-panel";
  panel.setAttribute("role", "dialog");
  panel.setAttribute("aria-modal", "true");
  panel.setAttribute("aria-labelledby", "shortcuts-title");

  const head = document.createElement("div");
  head.className = "shortcuts-head";

  const title = document.createElement("h2");
  title.id = "shortcuts-title";
  title.textContent = "Keyboard Shortcuts";

  const closeButton = document.createElement("button");
  closeButton.type = "button";
  closeButton.className = "icon-btn shortcuts-close";
  closeButton.dataset.action = "close-shortcuts";
  closeButton.setAttribute("aria-label", "Close shortcuts");

  const closeIcon = document.createElement("i");
  closeIcon.className = "ph ph-x";
  closeButton.appendChild(closeIcon);

  head.appendChild(title);
  head.appendChild(closeButton);
  panel.appendChild(head);

  const list = document.createElement("div");
  list.className = "shortcuts-list";

  rows.forEach(([labelText, keyText]) => {
    const row = document.createElement("div");
    row.className = "shortcut-row";

    const label = document.createElement("span");
    label.textContent = labelText;

    const key = document.createElement("kbd");
    key.textContent = keyText;

    row.appendChild(label);
    row.appendChild(key);
    list.appendChild(row);
  });

  panel.appendChild(list);
  overlay.appendChild(panel);
  document.body.appendChild(overlay);
  return overlay;
}

function openOverlay(overlay) {
  overlay.classList.add("is-open");
  overlay.setAttribute("aria-hidden", "false");
}

function closeOverlay(overlay) {
  overlay.classList.remove("is-open");
  overlay.setAttribute("aria-hidden", "true");
}

function defaultEscapeHandler() {
  document.dispatchEvent(new CustomEvent("biomni:escape"));
}

export function initShortcuts(options = {}) {
  const overlay = createOverlay();

  const closeButton = overlay.querySelector("[data-action='close-shortcuts']");
  const onOverlayClick = (event) => {
    if (event.target === overlay || event.target === closeButton || event.target.closest("[data-action='close-shortcuts']")) {
      closeOverlay(overlay);
    }
  };

  const onKeyDown = (event) => {
    if (event.defaultPrevented) return;

    if (event.key === "Escape") {
      closeOverlay(overlay);
      const onEscape = options.onEscape || defaultEscapeHandler;
      onEscape();
      return;
    }

    if (event.key === "Enter" && !event.shiftKey && !event.altKey && !event.ctrlKey && !event.metaKey) {
      // Enter handling is owned by chat input listeners; keep shortcuts passive.
      return;
    }

    if (!isModifierPressed(event)) return;

    const key = String(event.key || "").toLowerCase();

    if (key === "k" && !event.shiftKey) {
      event.preventDefault();
      if (typeof options.onFocusSearch === "function") {
        options.onFocusSearch();
      } else if (!focusSidebarSearch()) {
        const chatInput = resolveElement("#chat-input");
        if (chatInput) chatInput.focus();
      }
      return;
    }

    if (key === "n" && !event.shiftKey) {
      event.preventDefault();
      if (typeof options.onNewConversation === "function") {
        options.onNewConversation();
      }
      return;
    }

    if (key === "e" && event.shiftKey) {
      event.preventDefault();
      if (typeof options.onExport === "function") {
        options.onExport();
      }
      return;
    }

    if (keyIsSlash(event) && !event.shiftKey) {
      event.preventDefault();
      openOverlay(overlay);
    }
  };

  overlay.addEventListener("click", onOverlayClick);
  document.addEventListener("keydown", onKeyDown);

  return () => {
    overlay.removeEventListener("click", onOverlayClick);
    document.removeEventListener("keydown", onKeyDown);
    overlay.remove();
  };
}
