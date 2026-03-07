/* ==========================================================================
   Biomni Portal — export.js
   Conversation export helpers (Markdown download).
   ========================================================================== */

const DEFAULT_FILENAME_PREFIX = "biomni-conversation";

function resolveElement(target) {
  if (!target) return null;
  if (target instanceof HTMLElement) return target;
  if (typeof target === "string") return document.querySelector(target);
  return null;
}

function nowStamp(date = new Date()) {
  return new Date(date).toISOString();
}

function fileStamp(date = new Date()) {
  return nowStamp(date).replace(/[:.]/g, "-");
}

function sanitizeFileName(value) {
  return String(value || "")
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9-_]+/g, "-")
    .replace(/-+/g, "-")
    .replace(/^-|-$/g, "");
}

function defaultGetConversation() {
  if (window.biomniApp && typeof window.biomniApp.getCurrentConversation === "function") {
    return window.biomniApp.getCurrentConversation();
  }
  return null;
}

function defaultGetUser() {
  if (window.biomniApp && typeof window.biomniApp.getCurrentUser === "function") {
    return window.biomniApp.getCurrentUser();
  }
  return "scientist";
}

function defaultGetModel() {
  if (window.biomniApp && typeof window.biomniApp.getModelTag === "function") {
    return window.biomniApp.getModelTag();
  }
  const modelTag = document.querySelector(".model-tag");
  return modelTag ? modelTag.textContent.trim() : "unknown-model";
}

function normalizeMessages(conversation) {
  if (!conversation || !Array.isArray(conversation.messages)) return [];
  return conversation.messages
    .map((msg) => ({
      role: (msg && msg.role) || "",
      content: (msg && msg.content) || "",
    }))
    .filter((msg) => typeof msg.content === "string" && msg.content.trim());
}

function resolveRoleLabel(role) {
  return role === "user" ? "User" : "Biomni";
}

function normalizeText(value) {
  return String(value || "").replace(/\r\n/g, "\n").trim();
}

export function conversationToMarkdown(conversation, metadata = {}) {
  const date = metadata.date || new Date();
  const user = metadata.user || "scientist";
  const model = metadata.model || "unknown-model";
  const messages = normalizeMessages(conversation);

  const sections = messages.map((message) => {
    const roleLabel = resolveRoleLabel(message.role);
    return `## ${roleLabel}\n${normalizeText(message.content)}\n\n---`;
  });

  const header = [
    "---",
    `date: ${nowStamp(date)}`,
    `user: ${user}`,
    `model: ${model}`,
    "---",
    "",
    "# Biomni Conversation",
    "",
  ];

  return `${header.join("\n")}${sections.join("\n\n").trim()}\n`;
}

export function downloadMarkdown(markdown, filename) {
  const blob = new Blob([markdown], { type: "text/markdown;charset=utf-8" });
  const objectUrl = URL.createObjectURL(blob);

  const link = document.createElement("a");
  link.href = objectUrl;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();

  URL.revokeObjectURL(objectUrl);
}

export function exportConversation(options = {}) {
  const getConversation = options.getConversation || defaultGetConversation;
  const conversation = getConversation();
  const messages = normalizeMessages(conversation);

  if (!messages.length) {
    if (typeof options.onEmpty === "function") {
      options.onEmpty();
      return null;
    }
    window.alert("No messages available to export yet.");
    return null;
  }

  const metadata = {
    date: options.date || new Date(),
    user: (options.getUser || defaultGetUser)(),
    model: (options.getModel || defaultGetModel)(),
  };

  const markdown = conversationToMarkdown(conversation, metadata);
  const conversationId = sanitizeFileName(conversation.id || "");
  const baseName = sanitizeFileName(options.filenamePrefix || DEFAULT_FILENAME_PREFIX) || DEFAULT_FILENAME_PREFIX;
  const suffix = conversationId || fileStamp(metadata.date);
  const fileName = `${baseName}-${suffix}.md`;

  downloadMarkdown(markdown, fileName);
  return { fileName, markdown };
}

export function initExportFeature(options = {}) {
  const button = resolveElement(options.button || "#btn-export");
  if (!button) return () => {};

  const onClick = (event) => {
    event.preventDefault();
    const result = exportConversation(options);
    if (result) {
      document.dispatchEvent(new CustomEvent("biomni:conversation-exported", { detail: result }));
    }
  };

  button.addEventListener("click", onClick);

  return () => {
    button.removeEventListener("click", onClick);
  };
}
