function stringifyStepPayload(payload) {
  if (typeof payload === "string") return payload;
  if (!payload || typeof payload !== "object") return String(payload || "");

  const directText = payload.text || payload.step || payload.message || payload.output;
  if (typeof directText === "string") return directText;

  return JSON.stringify(payload, null, 2);
}

function parseAttributes(rawAttributes = "") {
  const attrs = {};
  const regex = /(\w+)=("([^"]*)"|'([^']*)')/g;
  let match = regex.exec(rawAttributes);

  while (match) {
    attrs[match[1].toLowerCase()] = match[3] || match[4] || "";
    match = regex.exec(rawAttributes);
  }

  return attrs;
}

function extractTag(source, tagName) {
  let regex = null;
  if (tagName === "execute") regex = /<execute([^>]*)>([\s\S]*?)<\/execute>/i;
  if (tagName === "observation") regex = /<observation([^>]*)>([\s\S]*?)<\/observation>/i;
  if (tagName === "solution") regex = /<solution([^>]*)>([\s\S]*?)<\/solution>/i;
  if (!regex) return null;

  const match = source.match(regex);
  if (!match) return null;

  return {
    attrs: parseAttributes(match[1]),
    content: match[2].trim(),
  };
}

function detectToolName(rawStep, executeTag) {
  if (executeTag?.attrs?.tool) return executeTag.attrs.tool;

  const patterns = [
    /tool\s*[:=]\s*([a-z0-9_.-]+)/i,
    /using\s+tool\s+([a-z0-9_.-]+)/i,
    /\b([a-z0-9_.-]+)\s+tool\b/i,
  ];

  for (const pattern of patterns) {
    const match = rawStep.match(pattern);
    if (match) return match[1];
  }

  return "";
}

export function parseToolStep(stepPayload) {
  const raw = stringifyStepPayload(stepPayload);
  const execute = extractTag(raw, "execute");
  const observation = extractTag(raw, "observation");
  const solution = extractTag(raw, "solution");

  let language = execute?.attrs?.lang || execute?.attrs?.language || "";
  if (!language && execute?.content) {
    const fenceMatch = execute.content.match(/^```([A-Za-z0-9_-]+)\n/);
    if (fenceMatch) language = fenceMatch[1];
  }

  const tool = detectToolName(raw, execute);

  return {
    raw,
    tool,
    language,
    execute: execute?.content || "",
    observation: observation?.content || "",
    solution: solution?.content || "",
  };
}

function createToolBadge(toolName) {
  const badge = document.createElement("span");
  badge.className = "tool-badge";
  badge.textContent = toolName;
  return badge;
}

function createExecuteBlock(step) {
  if (!step.execute) return null;

  const wrap = document.createElement("div");
  wrap.className = "tool-step tool-step-execute";

  const label = document.createElement("div");
  label.className = "tool-step-label";
  label.textContent = step.language ? `Execute (${step.language})` : "Execute";

  const pre = document.createElement("pre");
  const code = document.createElement("code");
  code.textContent = step.execute.replace(/^```[A-Za-z0-9_-]*\n?|```$/g, "").trim();
  pre.appendChild(code);

  wrap.appendChild(label);
  wrap.appendChild(pre);

  return wrap;
}

function createObservationBlock(step) {
  if (!step.observation) return null;

  const details = document.createElement("details");
  details.className = "tool-observation";

  const summary = document.createElement("summary");
  summary.textContent = "Observation";

  const content = document.createElement("div");
  content.className = "tool-observation-content";
  content.textContent = step.observation;

  details.appendChild(summary);
  details.appendChild(content);

  return details;
}

function createSolutionBlock(step) {
  if (!step.solution) return null;

  const block = document.createElement("div");
  block.className = "tool-step tool-step-solution";

  const label = document.createElement("div");
  label.className = "tool-step-label";
  label.textContent = "Solution";

  const body = document.createElement("div");
  body.className = "tool-step-solution-content";
  body.textContent = step.solution;

  block.appendChild(label);
  block.appendChild(body);

  return block;
}

function createFallbackBlock(step) {
  if (step.execute || step.observation || step.solution) return null;

  const block = document.createElement("div");
  block.className = "tool-step";
  block.textContent = step.raw;
  return block;
}

export function createToolPanel(messageElement) {
  const details = document.createElement("details");
  details.className = "tool-panel";
  details.open = true;

  const summary = document.createElement("summary");
  summary.className = "tool-panel-summary";

  const title = document.createElement("span");
  title.className = "tool-panel-title";
  title.textContent = "Agent Steps";

  const countBadge = document.createElement("span");
  countBadge.className = "tool-count-badge";
  countBadge.textContent = "0";

  summary.appendChild(title);
  summary.appendChild(countBadge);

  const content = document.createElement("div");
  content.className = "tool-panel-content";

  details.appendChild(summary);
  details.appendChild(content);
  messageElement.appendChild(details);

  let stepCount = 0;

  return {
    addStep(stepPayload) {
      const step = parseToolStep(stepPayload);
      stepCount += 1;
      countBadge.textContent = String(stepCount);

      const item = document.createElement("div");
      item.className = "tool-step-item";

      if (step.tool) {
        item.appendChild(createToolBadge(step.tool));
      }

      const execute = createExecuteBlock(step);
      const observation = createObservationBlock(step);
      const solution = createSolutionBlock(step);
      const fallback = createFallbackBlock(step);

      if (execute) item.appendChild(execute);
      if (observation) item.appendChild(observation);
      if (solution) item.appendChild(solution);
      if (fallback) item.appendChild(fallback);

      content.appendChild(item);
      return step;
    },
    getCount() {
      return stepCount;
    },
    removeIfEmpty() {
      if (stepCount === 0) details.remove();
    },
  };
}
