/* ==========================================================================
   Biomni Portal — share.js
   Conversation sharing (token request + modal with copyable link).
   ========================================================================== */

function resolveElement(target) {
  if (!target) return null;
  if (target instanceof HTMLElement) return target;
  if (typeof target === "string") return document.querySelector(target);
  return null;
}

async function copyToClipboard(text) {
  if (navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
    await navigator.clipboard.writeText(text);
    return true;
  }

  const probe = document.createElement("textarea");
  probe.value = text;
  probe.setAttribute("readonly", "true");
  probe.className = "clipboard-probe";
  document.body.appendChild(probe);
  probe.select();
  const ok = document.execCommand("copy");
  probe.remove();
  return ok;
}

function findToken(payload) {
  if (!payload || typeof payload !== "object") return "";
  return (
    payload.token ||
    payload.share_token ||
    payload.shareToken ||
    payload.id ||
    ""
  );
}

export async function requestShareToken(conversationId, fetchImpl = window.fetch) {
  if (!conversationId) {
    throw new Error("Missing conversation ID.");
  }

  const response = await fetchImpl(`/api/share/${encodeURIComponent(conversationId)}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ conversation_id: conversationId }),
  });

  const payload = await response.json().catch(() => ({}));
  const token = findToken(payload);

  if (!response.ok || !token) {
    const message = payload.error || payload.detail || `Share request failed (${response.status})`;
    throw new Error(message);
  }

  return token;
}

export function buildShareLink(token, origin = window.location.origin) {
  return `${origin}/shared/${encodeURIComponent(token)}`;
}

function ensureSidebarShareButtons(options = {}) {
  const sidebar = resolveElement(options.sidebarContainer) || document.querySelector(
    "#conversation-list, #sidebar-conversations, [data-role='conversation-list']"
  );
  if (!sidebar) return { sidebar: null, observer: null };

  const itemSelector = options.itemSelector || "[data-conversation-id]";

  const installButtons = () => {
    sidebar.querySelectorAll(itemSelector).forEach((item) => {
      if (!(item instanceof HTMLElement)) return;
      if (item.querySelector(".sidebar-share-btn")) return;

      const conversationId = item.dataset.conversationId || item.dataset.id || "";
      const button = document.createElement("button");
      button.type = "button";
      button.className = "sidebar-share-btn";
      button.dataset.action = "share-conversation";
      button.dataset.conversationId = conversationId;
      button.title = "Share conversation";
      button.setAttribute("aria-label", "Share conversation");
      button.innerHTML = '<i class="ph ph-share-network"></i>';
      item.appendChild(button);
    });
  };

  installButtons();

  const observer = new MutationObserver(() => installButtons());
  observer.observe(sidebar, { childList: true, subtree: true });

  return { sidebar, observer };
}

function createShareModal() {
  const overlay = document.createElement("div");
  overlay.className = "share-modal-overlay";
  overlay.setAttribute("aria-hidden", "true");
  overlay.innerHTML = `
    <div class="share-modal" role="dialog" aria-modal="true" aria-labelledby="share-title">
      <div class="share-modal-head">
        <h2 id="share-title">Share Conversation</h2>
        <button type="button" class="icon-btn share-modal-close" data-action="close-share-modal" aria-label="Close share modal">
          <i class="ph ph-x"></i>
        </button>
      </div>
      <p class="share-modal-copy">Anyone with this link can open a read-only shared view.</p>
      <div class="share-link-row">
        <input id="share-link-input" class="share-link-input" type="text" readonly aria-label="Share link" />
        <button type="button" class="share-copy-btn" data-action="copy-share-link">
          <i class="ph ph-copy"></i>
          Copy
        </button>
      </div>
      <p class="share-modal-status" id="share-modal-status"></p>
    </div>
  `;
  document.body.appendChild(overlay);

  const input = overlay.querySelector("#share-link-input");
  const status = overlay.querySelector("#share-modal-status");

  const setStatus = (text, isError = false) => {
    status.textContent = text;
    status.classList.toggle("is-error", isError);
    status.classList.toggle("is-success", !isError && Boolean(text));
  };

  const show = (link) => {
    overlay.classList.add("is-open");
    overlay.setAttribute("aria-hidden", "false");
    input.value = link;
    input.focus();
    input.select();
    setStatus("");
  };

  const hide = () => {
    overlay.classList.remove("is-open");
    overlay.setAttribute("aria-hidden", "true");
    setStatus("");
  };

  return { overlay, input, status, setStatus, show, hide };
}

function resolveConversationId(options, trigger) {
  if (trigger) {
    const explicitId = trigger.dataset.conversationId;
    if (explicitId) return explicitId;
    const item = trigger.closest("[data-conversation-id], [data-id]");
    if (item) return item.dataset.conversationId || item.dataset.id || "";
  }

  if (typeof options.getCurrentConversationId === "function") {
    return options.getCurrentConversationId() || "";
  }

  if (typeof options.getConversation === "function") {
    const conversation = options.getConversation();
    return (conversation && conversation.id) || "";
  }

  if (window.biomniApp && typeof window.biomniApp.getCurrentConversation === "function") {
    const conversation = window.biomniApp.getCurrentConversation();
    return (conversation && conversation.id) || "";
  }

  return "";
}

export function initShareFeature(options = {}) {
  const modal = createShareModal();
  const headerButton = resolveElement(options.headerButton || "#btn-share");
  const { sidebar, observer } = ensureSidebarShareButtons(options);

  const runShare = async (conversationId) => {
    modal.setStatus("Generating link...");
    try {
      const token = await requestShareToken(conversationId, options.fetchImpl || window.fetch.bind(window));
      const origin = typeof options.getOrigin === "function" ? options.getOrigin() : window.location.origin;
      const link = buildShareLink(token, origin);
      modal.show(link);
      modal.setStatus("Share link ready.");
    } catch (error) {
      modal.show("");
      modal.setStatus(error.message || "Could not create share link.", true);
    }
  };

  const onHeaderShare = (event) => {
    event.preventDefault();
    const conversationId = resolveConversationId(options);
    runShare(conversationId);
  };

  const onSidebarClick = (event) => {
    const trigger = event.target.closest("[data-action='share-conversation']");
    if (!trigger) return;
    event.preventDefault();
    event.stopPropagation();
    runShare(resolveConversationId(options, trigger));
  };

  const onOverlayClick = (event) => {
    if (event.target === modal.overlay || event.target.closest("[data-action='close-share-modal']")) {
      modal.hide();
      return;
    }

    if (event.target.closest("[data-action='copy-share-link']")) {
      const link = modal.input.value.trim();
      if (!link) {
        modal.setStatus("No link to copy yet.", true);
        return;
      }

      copyToClipboard(link)
        .then(() => {
          modal.setStatus("Link copied to clipboard.");
        })
        .catch(() => {
          modal.setStatus("Copy failed. You can still copy manually.", true);
        });
    }
  };

  const onEscape = () => modal.hide();
  const onRequestShare = () => {
    const conversationId = resolveConversationId(options);
    runShare(conversationId);
  };

  if (headerButton) headerButton.addEventListener("click", onHeaderShare);
  if (sidebar) sidebar.addEventListener("click", onSidebarClick);
  modal.overlay.addEventListener("click", onOverlayClick);
  document.addEventListener("biomni:escape", onEscape);
  document.addEventListener("biomni:request-share", onRequestShare);

  return () => {
    if (headerButton) headerButton.removeEventListener("click", onHeaderShare);
    if (sidebar) sidebar.removeEventListener("click", onSidebarClick);
    modal.overlay.removeEventListener("click", onOverlayClick);
    document.removeEventListener("biomni:escape", onEscape);
    document.removeEventListener("biomni:request-share", onRequestShare);
    if (observer) observer.disconnect();
    modal.overlay.remove();
  };
}
