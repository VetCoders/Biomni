import { getToken } from "./api.js";
import { renderMarkdown, sanitizeAgentOutput } from "./markdown.js";
import { createToolPanel } from "./tools.js";

function normalizeRole(role) {
  const value = String(role || "").toLowerCase();
  if (value === "assistant") return "agent";
  return value === "agent" || value === "user" ? value : "agent";
}

function extractText(payload) {
  if (payload === null || payload === undefined) return "";
  if (typeof payload === "string") return payload;
  if (typeof payload !== "object") return String(payload);

  const candidates = [
    payload.text,
    payload.delta,
    payload.output,
    payload.result,
    payload.content,
    payload.message,
    payload.error,
    payload.step,
  ];

  for (const candidate of candidates) {
    if (typeof candidate === "string" && candidate.trim()) {
      return candidate;
    }
  }

  return "";
}

function buildWsUrl() {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const url = new URL(`${protocol}//${window.location.host}/ws/stream`);

  const token = getToken();
  if (token) {
    // Browser WebSocket API cannot set arbitrary headers, so token goes in query.
    url.searchParams.set("token", token);
  }

  return url.toString();
}

function setMarkdownContent(element, markdownText) {
  const html = renderMarkdown(markdownText || "");
  const parser = new DOMParser();
  const doc = parser.parseFromString(`<div>${html}</div>`, "text/html");
  const container = doc.body.firstElementChild;
  element.replaceChildren(...(container ? Array.from(container.childNodes) : []));
}

export function createChat({
  chatAreaEl,
  messagesEl,
  welcomeEl,
  chatFormEl,
  chatInputEl,
  sendButtonEl,
  statusEl,
}) {
  let socket = null;
  let socketPromise = null;
  let reconnectTimer = null;
  let reconnectAttempts = 0;
  let manualClose = false;
  let activeSessionId = "";
  let inputEnabled = true;
  let streaming = false;
  let thinkingEl = null;

  let currentStream = null;

  function scrollToBottom() {
    chatAreaEl.scrollTop = chatAreaEl.scrollHeight;
  }

  function setStatus(text) {
    statusEl.textContent = text || "";
  }

  function setWelcomeVisible(visible) {
    welcomeEl.style.display = visible ? "" : "none";
  }

  function setInputEnabled(enabled) {
    inputEnabled = Boolean(enabled);
    chatInputEl.disabled = !inputEnabled;
    sendButtonEl.disabled = !inputEnabled || streaming;
  }

  function updateSendButtonState() {
    sendButtonEl.disabled = !inputEnabled || streaming;
  }

  function resizeInput() {
    chatInputEl.style.height = "auto";
    chatInputEl.style.height = `${Math.min(chatInputEl.scrollHeight, 160)}px`;
  }

  function clearComposer() {
    chatInputEl.value = "";
    resizeInput();
  }

  function showThinking() {
    hideThinking();

    const container = document.createElement("div");
    container.className = "thinking";
    container.innerHTML =
      '<div class="thinking-dots"><span></span><span></span><span></span></div><span>Agent is working...</span>';

    messagesEl.appendChild(container);
    thinkingEl = container;
    scrollToBottom();
  }

  function hideThinking() {
    if (!thinkingEl) return;
    thinkingEl.remove();
    thinkingEl = null;
  }

  function createMessageElement(role, content, extraClass = "") {
    const msgRole = normalizeRole(role);

    const container = document.createElement("div");
    container.className = `msg msg-${msgRole}`;
    if (extraClass) container.classList.add(extraClass);

    const label = document.createElement("div");
    label.className = "msg-label";
    label.textContent = msgRole === "user" ? "You" : "Biomni";

    const body = document.createElement("div");
    body.className = "msg-content";

    if (msgRole === "agent") {
      setMarkdownContent(body, content || "");
    } else {
      body.textContent = content || "";
    }

    container.appendChild(label);
    container.appendChild(body);

    return { container, body };
  }

  function appendUserMessage(content) {
    const msg = createMessageElement("user", content);
    messagesEl.appendChild(msg.container);
    setWelcomeVisible(false);
    scrollToBottom();
  }

  function appendAgentMessage(content, toolSteps = []) {
    const msg = createMessageElement("agent", content);
    messagesEl.appendChild(msg.container);

    if (Array.isArray(toolSteps) && toolSteps.length > 0) {
      const toolPanel = createToolPanel(msg.container);
      toolSteps.forEach((step) => toolPanel.addStep(step));
      toolPanel.removeIfEmpty();
    }

    setWelcomeVisible(false);
    scrollToBottom();
  }

  function appendErrorMessage(message) {
    const msg = createMessageElement("agent", message, "msg-error");
    messagesEl.appendChild(msg.container);
    setWelcomeVisible(false);
    scrollToBottom();
  }

  function clearMessages() {
    hideThinking();
    messagesEl.innerHTML = "";
    setWelcomeVisible(true);
  }

  function renderConversation(messages = []) {
    hideThinking();
    messagesEl.innerHTML = "";

    messages.forEach((message) => {
      const role = normalizeRole(message.role);
      if (role === "user") {
        appendUserMessage(message.content || "");
      } else {
        appendAgentMessage(message.content || "", message.toolSteps || message.steps || []);
      }
    });

    setWelcomeVisible(messages.length === 0);
    scrollToBottom();
  }

  function getOpenSocket() {
    if (socket && socket.readyState === WebSocket.OPEN) {
      return socket;
    }
    return null;
  }

  function resetSocketRefs() {
    socket = null;
    socketPromise = null;
  }

  function failActiveStream(reason) {
    if (!currentStream) return;

    const stream = currentStream;
    currentStream = null;

    hideThinking();
    streaming = false;
    updateSendButtonState();

    if (stream.rawText && stream.agentBody) {
      setMarkdownContent(stream.agentBody, sanitizeAgentOutput(stream.rawText));
    }

    const error = reason instanceof Error ? reason : new Error(String(reason || "Stream failed"));
    stream.reject(error);
  }

  function scheduleReconnect() {
    if (manualClose || reconnectTimer) return;

    reconnectAttempts += 1;
    const delay = Math.min(1000 * 2 ** Math.min(reconnectAttempts, 4), 10000);
    setStatus(`Connection lost. Reconnecting in ${Math.ceil(delay / 1000)}s...`);

    reconnectTimer = window.setTimeout(() => {
      reconnectTimer = null;
      connectSocket(activeSessionId).catch(() => {
        scheduleReconnect();
      });
    }, delay);
  }

  function handleSocketMessage(event) {
    let payload;
    try {
      payload = JSON.parse(event.data);
    } catch {
      return;
    }

    const type = payload.type || payload.event || "result";
    const data = payload.data ?? payload;

    if (!currentStream && type !== "error") {
      return;
    }

    if (type === "start") {
      showThinking();
      setStatus("Agent is thinking...");
      return;
    }

    if (type === "step") {
      hideThinking();
      if (!currentStream.agentContainer) {
        const agentMsg = createMessageElement("agent", "");
        messagesEl.appendChild(agentMsg.container);
        currentStream.agentContainer = agentMsg.container;
        currentStream.agentBody = agentMsg.body;
        currentStream.toolPanel = createToolPanel(agentMsg.container);
      }

      const stepPayload = data.step || data.text || data.output || data;
      const parsed = currentStream.toolPanel.addStep(stepPayload);
      currentStream.toolSteps.push(parsed);
      setWelcomeVisible(false);
      scrollToBottom();
      return;
    }

    if (type === "result") {
      hideThinking();
      if (!currentStream.agentContainer) {
        const agentMsg = createMessageElement("agent", "");
        messagesEl.appendChild(agentMsg.container);
        currentStream.agentContainer = agentMsg.container;
        currentStream.agentBody = agentMsg.body;
        currentStream.toolPanel = createToolPanel(agentMsg.container);
      }

      const chunk = extractText(data);
      if (chunk) {
        currentStream.rawText += chunk;
        const cleaned = sanitizeAgentOutput(currentStream.rawText);
        setMarkdownContent(currentStream.agentBody, cleaned);
      }

      setWelcomeVisible(false);
      scrollToBottom();
      return;
    }

    if (type === "error") {
      const errorText = extractText(data) || "Agent stream failed.";

      if (currentStream?.agentContainer) {
        currentStream.agentContainer.classList.add("msg-error");
        currentStream.agentBody.textContent = errorText;
      } else {
        appendErrorMessage(errorText);
      }

      setStatus("Error.");
      failActiveStream(new Error(errorText));
      return;
    }

    if (type === "end") {
      if (!currentStream) return;

      const stream = currentStream;
      currentStream = null;

      hideThinking();
      streaming = false;
      updateSendButtonState();

      const cleaned = sanitizeAgentOutput(stream.rawText);
      if (stream.agentBody && cleaned) {
        setMarkdownContent(stream.agentBody, cleaned);
      }

      stream.toolPanel?.removeIfEmpty();
      setStatus("Done.");
      stream.resolve({ content: cleaned, toolSteps: stream.toolSteps });
      return;
    }

    const fallbackText = extractText(data);
    if (fallbackText && currentStream?.agentBody) {
      currentStream.rawText += fallbackText;
      setMarkdownContent(currentStream.agentBody, sanitizeAgentOutput(currentStream.rawText));
      hideThinking();
      scrollToBottom();
    }
  }

  function connectSocket(sessionId = "") {
    activeSessionId = sessionId || activeSessionId;

    const existingSocket = getOpenSocket();
    if (existingSocket) {
      return Promise.resolve(existingSocket);
    }

    if (socketPromise) {
      return socketPromise;
    }

    manualClose = false;

    socketPromise = new Promise((resolve, reject) => {
      let opened = false;

      try {
        socket = new WebSocket(buildWsUrl());
      } catch (error) {
        resetSocketRefs();
        reject(error);
        return;
      }

      const handleOpen = () => {
        opened = true;
        reconnectAttempts = 0;
        setStatus("Connected.");
        resolve(socket);
      };

      const handleError = () => {
        // Browser does not provide useful details here; close handler drives reconnect flow.
      };

      const handleClose = () => {
        resetSocketRefs();

        if (!opened) {
          reject(new Error("WebSocket connection failed."));
        }

        if (currentStream) {
          failActiveStream(new Error("Connection interrupted during streaming."));
          appendErrorMessage("Connection interrupted. Please retry the prompt.");
        }

        if (!manualClose) {
          scheduleReconnect();
        }
      };

      socket.addEventListener("open", handleOpen, { once: true });
      socket.addEventListener("message", handleSocketMessage);
      socket.addEventListener("error", handleError);
      socket.addEventListener("close", handleClose);
    }).finally(() => {
      socketPromise = null;
    });

    return socketPromise;
  }

  async function streamAgentResponse({ text, sessionId }) {
    if (streaming) {
      throw new Error("Another response is still streaming.");
    }

    streaming = true;
    updateSendButtonState();
    setStatus("Connecting...");
    showThinking();

    const ws = await connectSocket(sessionId);

    return new Promise((resolve, reject) => {
      currentStream = {
        rawText: "",
        toolSteps: [],
        agentContainer: null,
        agentBody: null,
        toolPanel: null,
        resolve,
        reject,
      };

      try {
        ws.send(
          JSON.stringify({
            text,
            session_id: sessionId,
          }),
        );
      } catch (error) {
        failActiveStream(error);
      }
    });
  }

  function bindComposer(onSubmit) {
    async function submit(text) {
      const prompt = String(text || "").trim();
      if (!prompt || !inputEnabled || streaming) return;

      try {
        const shouldClear = await onSubmit(prompt);
        if (shouldClear !== false) {
          clearComposer();
        }
      } catch (error) {
        appendErrorMessage(error.message || "Failed to send message.");
      }
    }

    chatInputEl.addEventListener("input", resizeInput);

    chatInputEl.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        chatFormEl.requestSubmit();
      }
    });

    chatFormEl.addEventListener("submit", async (event) => {
      event.preventDefault();
      await submit(chatInputEl.value);
    });

    document.querySelectorAll(".preset-btn").forEach((button) => {
      button.addEventListener("click", async () => {
        const query = button.dataset.query;
        if (!query) return;
        await submit(query);
      });
    });

    resizeInput();
  }

  function destroy() {
    manualClose = true;
    hideThinking();

    if (reconnectTimer) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }

    if (socket) {
      socket.close();
      resetSocketRefs();
    }
  }

  return {
    bindComposer,
    appendUserMessage,
    appendAgentMessage,
    appendErrorMessage,
    renderConversation,
    clearMessages,
    setStatus,
    setInputEnabled,
    setWelcomeVisible,
    streamAgentResponse,
    focusInput() {
      chatInputEl.focus();
    },
    destroy,
  };
}
