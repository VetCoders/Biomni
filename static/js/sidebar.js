function isMobileViewport() {
  return window.matchMedia("(max-width: 768px)").matches;
}

function formatDate(value) {
  if (!value) return "";

  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";

  const now = new Date();
  const sameDay =
    now.getFullYear() === date.getFullYear() &&
    now.getMonth() === date.getMonth() &&
    now.getDate() === date.getDate();

  if (sameDay) {
    return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }

  return date.toLocaleDateString([], { month: "short", day: "numeric" });
}

function normalizeConversation(conversation) {
  return {
    id: String(conversation.id || conversation.session_id || ""),
    title: conversation.title || conversation.name || "Untitled conversation",
    updatedAt: conversation.updatedAt || conversation.updated_at || conversation.created_at || new Date().toISOString(),
    messageCount:
      conversation.messageCount ||
      conversation.message_count ||
      (Array.isArray(conversation.messages) ? conversation.messages.length : 0),
    isLocal: Boolean(conversation.isLocal),
  };
}

export function createSidebar({
  sidebarEl,
  listEl,
  searchInput,
  newChatButton,
  toggleButton,
  collapseButton,
  backdrop,
  onSelectConversation,
  onDeleteConversation,
  onNewConversation,
}) {
  let conversations = [];
  let activeConversationId = "";
  let searchQuery = "";

  function closeMobileSidebar() {
    document.body.classList.remove("sidebar-open");
  }

  function openMobileSidebar() {
    document.body.classList.add("sidebar-open");
  }

  function toggleSidebarVisibility() {
    if (isMobileViewport()) {
      document.body.classList.toggle("sidebar-open");
      return;
    }
    document.body.classList.toggle("sidebar-hidden");
  }

  function renderConversationItem(conversation) {
    const item = document.createElement("button");
    item.type = "button";
    item.className = "conversation-item";
    item.dataset.conversationId = conversation.id;
    if (conversation.id === activeConversationId) {
      item.classList.add("active");
    }

    const title = document.createElement("div");
    title.className = "conversation-title";
    title.textContent = conversation.title;

    const row = document.createElement("div");
    row.className = "conversation-row";

    const meta = document.createElement("div");
    meta.className = "conversation-meta";
    const dateLabel = formatDate(conversation.updatedAt);
    const count = Number(conversation.messageCount || 0);
    meta.textContent = count > 0 ? `${dateLabel} • ${count} msgs` : `${dateLabel} • Empty`;

    const deleteBtn = document.createElement("button");
    deleteBtn.type = "button";
    deleteBtn.className = "conversation-delete";
    deleteBtn.setAttribute("aria-label", "Delete conversation");
    deleteBtn.innerHTML = '<i class="ph ph-trash"></i>';

    deleteBtn.addEventListener("click", async (event) => {
      event.preventDefault();
      event.stopPropagation();
      await onDeleteConversation(conversation);
    });

    row.appendChild(meta);
    row.appendChild(deleteBtn);
    item.appendChild(title);
    item.appendChild(row);

    item.addEventListener("click", async () => {
      await onSelectConversation(conversation);
      closeMobileSidebar();
    });

    return item;
  }

  function render() {
    const normalized = conversations
      .map(normalizeConversation)
      .filter((conversation) => conversation.id)
      .filter((conversation) => {
        if (!searchQuery) return true;
        return conversation.title.toLowerCase().includes(searchQuery);
      });

    listEl.innerHTML = "";

    if (normalized.length === 0) {
      const empty = document.createElement("div");
      empty.className = "conversation-empty";
      empty.textContent = searchQuery
        ? "No conversations match your search."
        : "No conversations yet. Start a new chat.";
      listEl.appendChild(empty);
      return;
    }

    normalized.forEach((conversation) => {
      listEl.appendChild(renderConversationItem(conversation));
    });
  }

  toggleButton?.addEventListener("click", toggleSidebarVisibility);
  collapseButton?.addEventListener("click", toggleSidebarVisibility);
  backdrop?.addEventListener("click", closeMobileSidebar);

  newChatButton?.addEventListener("click", async () => {
    await onNewConversation();
    closeMobileSidebar();
  });

  searchInput?.addEventListener("input", () => {
    searchQuery = searchInput.value.trim().toLowerCase();
    render();
  });

  window.addEventListener("resize", () => {
    if (!isMobileViewport()) {
      closeMobileSidebar();
    }
  });

  return {
    setConversations(nextConversations, activeId = "") {
      conversations = Array.isArray(nextConversations) ? nextConversations : [];
      activeConversationId = activeId ? String(activeId) : "";
      render();
    },
    setActiveConversation(activeId) {
      activeConversationId = activeId ? String(activeId) : "";
      render();
    },
    openMobileSidebar,
    closeMobileSidebar,
    render,
  };
}
