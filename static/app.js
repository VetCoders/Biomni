/* ==========================================================================
   Biomni Portal — app.js
   Standalone chat interface for Stanford Biomni biomedical AI agent.
   VibeCrafted with AI Agents (c)2026 VetCoders
   ========================================================================== */

const $ = (id) => document.getElementById(id);

const chatArea = $("chat-area");
const messagesEl = $("messages");
const welcomeEl = $("welcome");
const chatForm = $("chat-form");
const chatInput = $("chat-input");
const btnSend = $("btn-send");
const btnTheme = $("btn-theme");
const btnClear = $("btn-clear");
const statusEl = $("status");

const messages = [];
let streaming = false;
let abortCtrl = null;

// ---------------------------------------------------------------------------
// Sanitize raw LangChain ReAct output
// ---------------------------------------------------------------------------

function sanitizeAgentOutput(raw) {
  if (!raw) return "";
  let text = raw;

  // Strip LangChain message delimiters
  text = text.replace(/={10,}\s*(Human|Ai|System)\s*Message\s*={10,}/gi, "");

  // Strip <solution>, <execute>, </solution>, </execute> tags
  text = text.replace(/<\/?(solution|execute)>/gi, "");

  // Strip "Thinking:" preamble blocks (everything before actual answer)
  text = text.replace(/^Thinking:[\s\S]*?(?=\n[A-Z]|\nJestem|\n\n)/i, "");

  // Strip repeated prompt echo (user message echoed back)
  // e.g. "kim jesteś?" appearing at the top of agent response
  text = text.replace(/^[^\n]{1,100}\??\s*={10,}[\s\S]*?={10,}\s*/m, "");

  // Clean up excessive whitespace
  text = text.replace(/\n{3,}/g, "\n\n").trim();

  return text;
}

// ---------------------------------------------------------------------------
// Theme
// ---------------------------------------------------------------------------

function getTheme() {
  return localStorage.getItem("biomni-theme") || "dark";
}

function setTheme(t) {
  document.documentElement.setAttribute("data-theme", t);
  localStorage.setItem("biomni-theme", t);
  const icon = btnTheme.querySelector("i");
  icon.className = t === "dark" ? "ph ph-sun" : "ph ph-moon";
}

setTheme(getTheme());

btnTheme.addEventListener("click", () => {
  setTheme(getTheme() === "dark" ? "light" : "dark");
});

// ---------------------------------------------------------------------------
// Auto-resize textarea
// ---------------------------------------------------------------------------

chatInput.addEventListener("input", () => {
  chatInput.style.height = "auto";
  chatInput.style.height = Math.min(chatInput.scrollHeight, 160) + "px";
});

// ---------------------------------------------------------------------------
// Markdown (lightweight)
// ---------------------------------------------------------------------------

function renderMarkdown(text) {
  if (!text) return "";
  let html = text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");

  // Code blocks
  html = html.replace(/```(\w*)\n([\s\S]*?)```/g, (_, lang, code) => {
    return `<pre><code>${code.trim()}</code></pre>`;
  });

  // Inline code
  html = html.replace(/`([^`]+)`/g, "<code>$1</code>");

  // Headers
  html = html.replace(/^### (.+)$/gm, "<strong>$1</strong>");
  html = html.replace(/^## (.+)$/gm, "<strong>$1</strong>");
  html = html.replace(/^# (.+)$/gm, "<strong>$1</strong>");

  // Bold / italic
  html = html.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  html = html.replace(/\*(.+?)\*/g, "<em>$1</em>");

  // Links
  html = html.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');

  // Lists
  html = html.replace(/^[-*] (.+)$/gm, "<li>$1</li>");
  html = html.replace(/(<li>.*<\/li>\n?)+/g, (m) => `<ul>${m}</ul>`);

  // Numbered lists
  html = html.replace(/^\d+\. (.+)$/gm, "<li>$1</li>");

  // Paragraphs
  html = html.replace(/\n{2,}/g, "</p><p>");
  html = `<p>${html}</p>`;
  html = html.replace(/<p>\s*<\/p>/g, "");

  return html;
}

// ---------------------------------------------------------------------------
// Message rendering
// ---------------------------------------------------------------------------

function createMsgEl(role, content, extra) {
  const div = document.createElement("div");
  div.className = `msg msg-${role}`;
  if (extra) div.className += ` ${extra}`;

  const label = document.createElement("div");
  label.className = "msg-label";
  label.textContent = role === "user" ? "You" : "Biomni";
  div.appendChild(label);

  const body = document.createElement("div");
  body.className = "msg-content";
  if (role === "agent") {
    body.innerHTML = renderMarkdown(content);
  } else {
    body.textContent = content;
  }
  div.appendChild(body);

  return div;
}

function showThinking() {
  const div = document.createElement("div");
  div.className = "thinking";
  div.id = "thinking";
  div.innerHTML = `<div class="thinking-dots"><span></span><span></span><span></span></div><span>Agent is working...</span>`;
  messagesEl.appendChild(div);
  scrollToBottom();
}

function hideThinking() {
  const el = $("thinking");
  if (el) el.remove();
}

function scrollToBottom() {
  chatArea.scrollTop = chatArea.scrollHeight;
}

function setStatus(text) {
  statusEl.textContent = text;
}

// ---------------------------------------------------------------------------
// Chat
// ---------------------------------------------------------------------------

async function sendMessage(prompt) {
  if (streaming || !prompt.trim()) return;

  // Hide welcome
  welcomeEl.style.display = "none";

  // User message
  messages.push({ role: "user", content: prompt });
  messagesEl.appendChild(createMsgEl("user", prompt));
  scrollToBottom();

  // Prepare
  streaming = true;
  btnSend.disabled = true;
  chatInput.value = "";
  chatInput.style.height = "auto";
  setStatus("");
  showThinking();

  abortCtrl = new AbortController();
  let fullText = "";
  let agentEl = null;
  let agentBody = null;
  let toolSteps = [];

  try {
    const resp = await fetch("/api/chat/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ prompt }),
      signal: abortCtrl.signal,
    });

    if (!resp.ok) {
      hideThinking();
      const err = await resp.json().catch(() => ({ error: resp.statusText }));
      const errEl = createMsgEl("agent", err.error || "Connection error", "msg-error");
      messagesEl.appendChild(errEl);
      scrollToBottom();
      return;
    }

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      const lines = buffer.split("\n");
      buffer = lines.pop() || "";

      for (const line of lines) {
        if (!line.startsWith("data:")) continue;
        const raw = line.slice(5).trim();
        if (!raw || raw === "[DONE]") continue;

        let data;
        try {
          data = JSON.parse(raw);
        } catch {
          continue;
        }

        // Error
        if (data.error) {
          hideThinking();
          const errEl = createMsgEl("agent", data.error, "msg-error");
          messagesEl.appendChild(errEl);
          scrollToBottom();
          continue;
        }

        // Tool calls / steps
        if (data.type === "tool_call" || data.type === "tool_result" || data.step) {
          hideThinking();
          const stepText = data.tool || data.step || data.type || "working...";
          const stepEl = document.createElement("div");
          stepEl.className = "tool-step";
          stepEl.textContent = typeof stepText === "string" ? stepText : JSON.stringify(stepText);
          if (data.input) {
            stepEl.textContent += `: ${typeof data.input === "string" ? data.input : JSON.stringify(data.input)}`;
          }
          messagesEl.appendChild(stepEl);
          toolSteps.push(stepEl);
          scrollToBottom();
          continue;
        }

        // Delta text
        const delta = data.delta || data.text || data.output;
        if (delta && typeof delta === "string") {
          hideThinking();

          if (!agentEl) {
            agentEl = createMsgEl("agent", "");
            agentBody = agentEl.querySelector(".msg-content");
            messagesEl.appendChild(agentEl);
          }
          fullText += delta;
          const cleaned = sanitizeAgentOutput(fullText);
          if (cleaned) {
            agentBody.innerHTML = renderMarkdown(cleaned);
            scrollToBottom();
          }
        }

        // Done
        if (data.type === "done" || data.done) {
          setStatus("Done.");
        }
      }
    }
  } catch (err) {
    if (err.name === "AbortError") {
      setStatus("Cancelled.");
    } else {
      hideThinking();
      const errEl = createMsgEl("agent", `Error: ${err.message}`, "msg-error");
      messagesEl.appendChild(errEl);
    }
  } finally {
    hideThinking();
    streaming = false;
    btnSend.disabled = false;
    abortCtrl = null;

    if (fullText) {
      messages.push({ role: "agent", content: sanitizeAgentOutput(fullText) });
    } else if (!fullText && messagesEl.querySelectorAll(".msg-error").length === 0) {
      // No response received — show fallback
      const noResp = createMsgEl("agent", "No response received from agent.", "msg-error");
      messagesEl.appendChild(noResp);
    }
    scrollToBottom();
  }
}

// ---------------------------------------------------------------------------
// Events
// ---------------------------------------------------------------------------

chatForm.addEventListener("submit", (e) => {
  e.preventDefault();
  const prompt = chatInput.value.trim();
  if (prompt) sendMessage(prompt);
});

chatInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    chatForm.requestSubmit();
  }
});

btnClear.addEventListener("click", () => {
  messages.length = 0;
  messagesEl.innerHTML = "";
  welcomeEl.style.display = "";
  setStatus("");
  chatInput.focus();
});

// Preset buttons
document.querySelectorAll(".preset-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    const query = btn.dataset.query;
    if (query) sendMessage(query);
  });
});
