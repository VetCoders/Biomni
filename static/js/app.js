import { apiFetch, clearToken, getToken, setToken, setUnauthorizedHandler } from "./api.js";
import { createAuth } from "./auth.js";
import { createChat } from "./chat.js";
import { createSidebar } from "./sidebar.js";
import { initExportFeature } from "./export.js";
import { initShortcuts } from "./shortcuts.js";
import { initShareFeature } from "./share.js";

const THEME_KEY = "biomni-theme";
const FEATURE_FALLBACK_CATEGORIES = [
  "Literature Search",
  "Clinical Trials",
  "Pharmacology",
  "Genomics",
  "Immunology",
  "Mechanism Discovery",
];
const featureCleanup = [];

const state = {
  user: null,
  token: getToken(),
  conversations: [],
  currentConversation: null,
  messages: [],
  tools: [],
  theme: localStorage.getItem(THEME_KEY) || "dark",
};

function createEventBus() {
  const listeners = new Map();

  return {
    emit(event, payload) {
      const handlers = listeners.get(event);
      if (!handlers) return;
      handlers.forEach((handler) => handler(payload));
    },
    subscribe(event, handler) {
      if (!listeners.has(event)) {
        listeners.set(event, new Set());
      }
      listeners.get(event).add(handler);
      return () => listeners.get(event)?.delete(handler);
    },
  };
}

const bus = createEventBus();

function setState(patch) {
  Object.assign(state, patch);
  bus.emit("state:change", { ...state });
}

function buildConversationTitle(prompt) {
  const text = String(prompt || "").trim().replace(/\s+/g, " ");
  if (!text) return "New conversation";
  return text.length > 72 ? `${text.slice(0, 69)}...` : text;
}

function normalizeUser(payload) {
  const source = payload?.user || payload || {};

  return {
    id: source.id || source.user_id || "",
    display_name: source.display_name || source.name || source.email || "Scientist",
    email: source.email || "",
  };
}

function normalizeConversation(item) {
  return {
    id: String(item.id || item.session_id || ""),
    title: item.title || item.name || "Untitled conversation",
    updatedAt: item.updatedAt || item.updated_at || item.created_at || new Date().toISOString(),
    createdAt: item.createdAt || item.created_at || item.updated_at || new Date().toISOString(),
    messageCount:
      item.messageCount ||
      item.message_count ||
      (Array.isArray(item.messages) ? item.messages.length : 0),
    isLocal: Boolean(item.isLocal),
  };
}

function normalizeConversations(payload) {
  const list = Array.isArray(payload)
    ? payload
    : payload?.conversations || payload?.items || payload?.data || [];

  if (!Array.isArray(list)) return [];

  return list
    .map(normalizeConversation)
    .filter((item) => item.id)
    .sort((a, b) => new Date(b.updatedAt).getTime() - new Date(a.updatedAt).getTime());
}

function normalizeMessage(message) {
  const roleRaw = String(message.role || message.sender || "agent").toLowerCase();
  const role = roleRaw === "assistant" ? "agent" : roleRaw === "user" ? "user" : "agent";

  return {
    id: String(message.id || message.message_id || `${role}-${Date.now()}-${Math.random()}`),
    role,
    content: message.content || message.text || message.output || "",
    createdAt: message.createdAt || message.created_at || new Date().toISOString(),
    toolSteps: message.toolSteps || message.tool_steps || message.steps || [],
  };
}

function normalizeMessages(payload) {
  const list = Array.isArray(payload)
    ? payload
    : payload?.messages || payload?.conversation?.messages || payload?.data?.messages || [];

  if (!Array.isArray(list)) return [];
  return list.map(normalizeMessage);
}

const elements = {
  themeButton: document.getElementById("btn-theme"),
  themeIcon: document.querySelector("#btn-theme i"),
  userDisplay: document.getElementById("user-display"),
  logoutButton: document.getElementById("btn-logout"),
  exportButton: document.getElementById("btn-export"),
  shareButton: document.getElementById("btn-share"),
  modelTag: document.querySelector(".model-tag"),

  chatArea: document.getElementById("chat-area"),
  messages: document.getElementById("messages"),
  welcome: document.getElementById("welcome"),
  chatForm: document.getElementById("chat-form"),
  chatInput: document.getElementById("chat-input"),
  sendButton: document.getElementById("btn-send"),
  status: document.getElementById("status"),

  sidebar: document.getElementById("sidebar"),
  conversationList: document.getElementById("conversation-list"),
  conversationSearch: document.getElementById("conversation-search"),
  newChatButton: document.getElementById("btn-new-chat"),
  sidebarToggleButton: document.getElementById("btn-sidebar-toggle"),
  sidebarCollapseButton: document.getElementById("btn-sidebar-collapse"),
  sidebarBackdrop: document.getElementById("sidebar-backdrop"),

  authModal: document.getElementById("auth-modal"),
  authClose: document.getElementById("auth-close"),
  authError: document.getElementById("auth-error"),
  authTabLogin: document.getElementById("auth-tab-login"),
  authTabRegister: document.getElementById("auth-tab-register"),
  loginForm: document.getElementById("login-form"),
  registerForm: document.getElementById("register-form"),
  capabilitiesGrid: document.getElementById("capabilities-grid"),
};

function getCurrentConversationSnapshot() {
  return {
    id: state.currentConversation?.id || "",
    title: state.currentConversation?.title || "Biomni conversation",
    messages: state.messages.map((message) => ({
      role: message.role,
      content: message.content,
    })),
  };
}

function getCurrentUserLabel() {
  return state.user?.display_name || "scientist";
}

function getModelTagLabel() {
  return elements.modelTag?.textContent?.trim() || "Stanford Biomni + Claude";
}

function focusConversationSearch() {
  if (elements.conversationSearch instanceof HTMLElement) {
    elements.conversationSearch.focus();
    if ("select" in elements.conversationSearch && typeof elements.conversationSearch.select === "function") {
      elements.conversationSearch.select();
    }
    return true;
  }
  return false;
}

function titleCase(value) {
  return String(value || "")
    .replace(/[_-]+/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function capabilityIcon(category) {
  const normalized = String(category || "").toLowerCase();
  if (normalized.includes("pubmed") || normalized.includes("literature")) return "ph-books";
  if (normalized.includes("trial")) return "ph-clipboard-text";
  if (normalized.includes("drug") || normalized.includes("pharma") || normalized.includes("admet")) return "ph-pill";
  if (normalized.includes("genom") || normalized.includes("crispr")) return "ph-dna";
  if (normalized.includes("immun")) return "ph-shield-plus";
  if (normalized.includes("protein") || normalized.includes("molecular")) return "ph-atom";
  return "ph-circles-three-plus";
}

function extractCapabilityCategories(payload) {
  if (!payload) return [];

  if (Array.isArray(payload)) {
    return payload
      .map((item) => {
        if (typeof item === "string") return item;
        if (item && typeof item === "object") {
          return item.category || item.group || item.domain || item.name || "";
        }
        return "";
      })
      .filter(Boolean);
  }

  if (payload.tools) return extractCapabilityCategories(payload.tools);
  if (payload.data) return extractCapabilityCategories(payload.data);
  if (payload.categories) return extractCapabilityCategories(payload.categories);

  return [];
}

function renderCapabilities(categories) {
  if (!elements.capabilitiesGrid) return;

  const unique = [...new Set(categories.map((category) => titleCase(category)).filter(Boolean))].slice(0, 12);
  const list = unique.length > 0 ? unique : FEATURE_FALLBACK_CATEGORIES;
  elements.capabilitiesGrid.innerHTML = "";

  list.forEach((category) => {
    const card = document.createElement("div");
    card.className = "capability-card";

    const icon = document.createElement("i");
    icon.className = `ph ${capabilityIcon(category)} capability-icon`;
    icon.setAttribute("aria-hidden", "true");

    const label = document.createElement("span");
    label.textContent = category;

    card.appendChild(icon);
    card.appendChild(label);
    elements.capabilitiesGrid.appendChild(card);
  });
}

async function loadCapabilities() {
  if (!elements.capabilitiesGrid) return;

  try {
    const payload = await apiFetch("/api/tools");
    renderCapabilities(extractCapabilityCategories(payload));
  } catch {
    renderCapabilities(FEATURE_FALLBACK_CATEGORIES);
  }
}

function initFeatureModules() {
  featureCleanup.push(
    initExportFeature({
      button: elements.exportButton,
      getConversation: getCurrentConversationSnapshot,
      getUser: getCurrentUserLabel,
      getModel: getModelTagLabel,
      onEmpty: () => chat.setStatus("Nothing to export yet."),
    })
  );

  featureCleanup.push(
    initShareFeature({
      headerButton: elements.shareButton,
      sidebarContainer: elements.conversationList,
      itemSelector: ".conversation-item",
      getCurrentConversationId: () => state.currentConversation?.id || "",
      getConversation: getCurrentConversationSnapshot,
    })
  );

  featureCleanup.push(
    initShortcuts({
      onFocusSearch: () => {
        if (!focusConversationSearch()) chat.focusInput();
      },
      onNewConversation: () => {
        createNewConversation();
      },
      onExport: () => {
        elements.exportButton?.click();
      },
      onEscape: () => {
        document.dispatchEvent(new CustomEvent("biomni:escape"));
      },
    })
  );

  window.addEventListener(
    "beforeunload",
    () => {
      featureCleanup.forEach((cleanup) => {
        if (typeof cleanup === "function") cleanup();
      });
    },
    { once: true }
  );
}

function applyTheme(theme) {
  const nextTheme = theme === "light" ? "light" : "dark";
  document.documentElement.setAttribute("data-theme", nextTheme);
  localStorage.setItem(THEME_KEY, nextTheme);

  if (elements.themeIcon) {
    elements.themeIcon.className = nextTheme === "dark" ? "ph ph-sun" : "ph ph-moon";
  }

  setState({ theme: nextTheme });
}

const chat = createChat({
  chatAreaEl: elements.chatArea,
  messagesEl: elements.messages,
  welcomeEl: elements.welcome,
  chatFormEl: elements.chatForm,
  chatInputEl: elements.chatInput,
  sendButtonEl: elements.sendButton,
  statusEl: elements.status,
});

const sidebar = createSidebar({
  sidebarEl: elements.sidebar,
  listEl: elements.conversationList,
  searchInput: elements.conversationSearch,
  newChatButton: elements.newChatButton,
  toggleButton: elements.sidebarToggleButton,
  collapseButton: elements.sidebarCollapseButton,
  backdrop: elements.sidebarBackdrop,
  onSelectConversation: async (conversation) => {
    await selectConversation(conversation.id);
  },
  onDeleteConversation: async (conversation) => {
    const approved = window.confirm("Delete this conversation?");
    if (!approved) return;
    await deleteConversation(conversation.id);
  },
  onNewConversation: async () => {
    createNewConversation();
  },
});

const auth = createAuth({
  modal: elements.authModal,
  closeButton: elements.authClose,
  errorEl: elements.authError,
  loginTab: elements.authTabLogin,
  registerTab: elements.authTabRegister,
  loginForm: elements.loginForm,
  registerForm: elements.registerForm,
  onAuthenticated: async ({ token, user }) => {
    setToken(token);
    setState({ token, user });
    auth.setLocked(false);
    chat.setInputEnabled(true);
    await loadConversations();

    if (state.conversations.length > 0) {
      await selectConversation(state.conversations[0].id);
    } else {
      createNewConversation();
    }
  },
  onLoggedOut: () => {
    setState({
      token: "",
      user: null,
      conversations: [],
      currentConversation: null,
      messages: [],
      tools: [],
    });

    chat.clearMessages();
    chat.setStatus("");
    chat.setInputEnabled(false);
    sidebar.setConversations([], "");
  },
});

function updateHeader() {
  elements.userDisplay.textContent = state.user?.display_name || "Guest";
  elements.logoutButton.classList.toggle("hidden", !state.user);
}

function summarizeTools(messages) {
  return messages.flatMap((message) => (Array.isArray(message.toolSteps) ? message.toolSteps : []));
}

function createNewConversation(initialPrompt = "") {
  const id =
    typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
      ? `local-${crypto.randomUUID()}`
      : `local-${Date.now()}`;

  const conversation = normalizeConversation({
    id,
    title: initialPrompt ? buildConversationTitle(initialPrompt) : "New conversation",
    messageCount: 0,
    isLocal: true,
    updatedAt: new Date().toISOString(),
  });

  const remaining = state.conversations.filter((item) => item.id !== conversation.id);
  const conversations = [conversation, ...remaining];

  setState({
    conversations,
    currentConversation: conversation,
    messages: [],
    tools: [],
  });

  sidebar.setConversations(conversations, conversation.id);
  chat.clearMessages();
  chat.setStatus("");
  chat.focusInput();

  return conversation;
}

async function loadConversations() {
  try {
    const payload = await apiFetch("/api/conversations");
    const conversations = normalizeConversations(payload);
    setState({ conversations });
    sidebar.setConversations(conversations, state.currentConversation?.id || "");
  } catch (error) {
    chat.appendErrorMessage(error.message || "Failed to load conversations.");
    setState({ conversations: [] });
    sidebar.setConversations([], "");
  }
}

async function selectConversation(conversationId) {
  const target = state.conversations.find((item) => item.id === String(conversationId));
  if (!target) return;

  setState({ currentConversation: target, messages: [], tools: [] });
  sidebar.setActiveConversation(target.id);
  chat.setStatus("Loading conversation...");

  if (target.isLocal) {
    chat.renderConversation([]);
    chat.setStatus("");
    return;
  }

  try {
    const payload = await apiFetch(`/api/conversations/${encodeURIComponent(target.id)}`);
    const messages = normalizeMessages(payload);
    const tools = summarizeTools(messages);

    setState({ messages, tools, currentConversation: target });
    chat.renderConversation(messages);
    chat.setStatus("");
  } catch (error) {
    chat.appendErrorMessage(error.message || "Could not load conversation.");
    chat.renderConversation([]);
    chat.setStatus("Failed to load conversation.");
  }
}

async function deleteConversation(conversationId) {
  const id = String(conversationId || "");
  if (!id) return;

  const isLocal = id.startsWith("local-");

  if (!isLocal) {
    await apiFetch(`/api/conversations/${encodeURIComponent(id)}`, { method: "DELETE" });
  }

  const conversations = state.conversations.filter((conversation) => conversation.id !== id);
  const deletedWasActive = state.currentConversation?.id === id;

  setState({ conversations });
  sidebar.setConversations(conversations, deletedWasActive ? "" : state.currentConversation?.id || "");

  if (!deletedWasActive) return;

  if (conversations.length > 0) {
    await selectConversation(conversations[0].id);
  } else {
    createNewConversation();
  }
}

function updateConversationAfterMessage(conversation, prompt, delta = 1) {
  const nextConversation = normalizeConversation({
    ...conversation,
    title:
      conversation.title && conversation.title !== "New conversation"
        ? conversation.title
        : buildConversationTitle(prompt),
    updatedAt: new Date().toISOString(),
    messageCount: Number(conversation.messageCount || 0) + delta,
    isLocal: false,
  });

  const otherConversations = state.conversations.filter((item) => item.id !== nextConversation.id);
  const conversations = [nextConversation, ...otherConversations];

  setState({ conversations, currentConversation: nextConversation });
  sidebar.setConversations(conversations, nextConversation.id);
}

async function sendPrompt(prompt) {
  if (!state.user) {
    auth.show("login", { locked: true });
    chat.setStatus("Login required.");
    return false;
  }

  let conversation = state.currentConversation;
  if (!conversation) {
    conversation = createNewConversation(prompt);
  }

  const userMessage = normalizeMessage({
    role: "user",
    content: prompt,
    created_at: new Date().toISOString(),
  });

  const nextMessages = [...state.messages, userMessage];
  setState({ messages: nextMessages });

  chat.appendUserMessage(prompt);
  updateConversationAfterMessage(conversation, prompt, 1);

  try {
    const stream = await chat.streamAgentResponse({
      text: prompt,
      sessionId: conversation.id,
    });

    // Update local conversation ID with server-assigned one
    if (stream.conversationId && conversation.id !== stream.conversationId) {
      const serverConv = normalizeConversation({
        ...conversation,
        id: stream.conversationId,
        isLocal: false,
      });
      const others = state.conversations.filter(
        (c) => c.id !== conversation.id && c.id !== stream.conversationId,
      );
      const conversations = [serverConv, ...others];
      setState({ conversations, currentConversation: serverConv });
      sidebar.setConversations(conversations, serverConv.id);
    }

    if (!stream.content) {
      return true;
    }

    const agentMessage = normalizeMessage({
      role: "agent",
      content: stream.content,
      tool_steps: stream.toolSteps,
      created_at: new Date().toISOString(),
    });

    const updatedMessages = [...state.messages, agentMessage];
    const tools = summarizeTools(updatedMessages);

    setState({ messages: updatedMessages, tools });
    updateConversationAfterMessage(state.currentConversation, prompt, 1);
    return true;
  } catch (error) {
    // Error message already displayed by WebSocket handler — just update status
    chat.setStatus("Error.");
    return false;
  }
}

async function bootstrap() {
  applyTheme(state.theme);
  updateHeader();
  loadCapabilities();
  initFeatureModules();

  chat.bindComposer(sendPrompt);
  chat.setInputEnabled(false);

  elements.themeButton.addEventListener("click", () => {
    applyTheme(state.theme === "dark" ? "light" : "dark");
  });

  elements.logoutButton.addEventListener("click", () => {
    auth.logout();
  });

  setUnauthorizedHandler(() => {
    clearToken();
    setState({ token: "", user: null });
    chat.setInputEnabled(false);
    auth.show("login", { locked: true });
  });

  if (!state.token) {
    auth.show("login", { locked: true });
    return;
  }

  try {
    const me = await apiFetch("/api/auth/me");
    const user = normalizeUser(me);
    setState({ user, token: state.token });

    chat.setInputEnabled(true);
    auth.setLocked(false);
    await loadConversations();

    if (state.conversations.length > 0) {
      await selectConversation(state.conversations[0].id);
    } else {
      createNewConversation();
    }
  } catch {
    clearToken();
    setState({ token: "", user: null });
    chat.setInputEnabled(false);
    auth.show("login", { locked: true });
  }
}

bus.subscribe("state:change", () => {
  updateHeader();
  sidebar.setConversations(state.conversations, state.currentConversation?.id || "");
});

bootstrap();
