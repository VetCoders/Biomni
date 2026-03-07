function escapeHtml(value) {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/\"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function sanitizeUrl(rawUrl) {
  const candidate = rawUrl.replace(/&amp;/g, "&").trim();

  try {
    const parsed = new URL(candidate, window.location.origin);
    if (["http:", "https:", "mailto:"].includes(parsed.protocol)) {
      return parsed.toString();
    }
  } catch {
    return "#";
  }

  return "#";
}

function splitTrailingPunctuation(value) {
  const match = value.match(/^(.*?)([.,;:]+)?$/);
  if (!match) return { core: value, trailing: "" };
  return { core: match[1], trailing: match[2] || "" };
}

function citationAnchor(kind, label, url) {
  return `<a class="citation-link citation-${kind}" href="${url}" target="_blank" rel="noopener noreferrer">${label}</a>`;
}

function replaceCitations(html) {
  let output = html;

  output = output.replace(/\bPMID:(\d+)\b/gi, (_, pmid) => {
    const label = `PMID:${pmid}`;
    return citationAnchor("pmid", label, `https://pubmed.ncbi.nlm.nih.gov/${pmid}/`);
  });

  output = output.replace(/\barXiv:(\d{4}\.\d{4,5}(?:v\d+)?)\b/gi, (_, arxivId) => {
    const label = `arXiv:${arxivId}`;
    return citationAnchor("arxiv", label, `https://arxiv.org/abs/${arxivId}`);
  });

  output = output.replace(/\b(10\.\d{4,9}\/[A-Za-z0-9._;()/:+-]+)\b/g, (match) => {
    const { core, trailing } = splitTrailingPunctuation(match);
    return `${citationAnchor("doi", core, `https://doi.org/${core}`)}${trailing}`;
  });

  return output;
}

function withPlaceholders(source, regex, factory) {
  const placeholders = [];
  const text = source.replace(regex, (...args) => {
    const replacement = factory(...args);
    const token = `%%PLACEHOLDER_${placeholders.length}%%`;
    placeholders.push(replacement);
    return token;
  });

  return {
    text,
    restore(value) {
      return value.replace(/%%PLACEHOLDER_(\d+)%%/g, (_, index) => placeholders[Number(index)] || "");
    },
  };
}

function renderParagraphs(markup) {
  const blocks = markup
    .split(/\n{2,}/)
    .map((block) => block.trim())
    .filter(Boolean);

  return blocks
    .map((block) => {
      if (/^<(h[1-3]|ul|ol|pre|blockquote|details|p|div)\b/i.test(block)) {
        return block;
      }
      return `<p>${block.replace(/\n/g, "<br/>")}</p>`;
    })
    .join("");
}

// Keeps the same cleanup logic used in the legacy single-file app.
export function sanitizeAgentOutput(raw) {
  if (!raw) return "";
  let text = raw;

  text = text.replace(/={10,}\s*(Human|Ai|System)\s*Message\s*={10,}/gi, "");
  text = text.replace(/<\/?(solution|execute)>/gi, "");
  text = text.replace(/^Thinking:[\s\S]*?(?=\n[A-Z]|\nJestem|\n\n)/i, "");
  text = text.replace(/^[^\n]{1,100}\??\s*={10,}[\s\S]*?={10,}\s*/m, "");
  text = text.replace(/\n{3,}/g, "\n\n").trim();

  return text;
}

export function renderMarkdown(text) {
  if (!text) return "";

  const normalized = text.replace(/\r\n?/g, "\n");
  let html = escapeHtml(normalized);

  const codeBlocks = withPlaceholders(html, /```([A-Za-z0-9_-]*)\n([\s\S]*?)```/g, (_, lang, code) => {
    const language = (lang || "").trim();
    const languageTag = language ? `<div class="code-lang">${language}</div>` : "";
    return `<pre>${languageTag}<code>${code.trim()}</code></pre>`;
  });
  html = codeBlocks.text;

  const markdownLinks = withPlaceholders(html, /\[([^\]]+)\]\(([^)]+)\)/g, (_, label, href) => {
    const safeHref = sanitizeUrl(href);
    return `<a href="${safeHref}" target="_blank" rel="noopener noreferrer">${label}</a>`;
  });
  html = markdownLinks.text;

  const inlineCode = withPlaceholders(html, /`([^`]+)`/g, (_, code) => `<code>${code}</code>`);
  html = inlineCode.text;

  html = html.replace(/^###\s+(.+)$/gm, "<h3>$1</h3>");
  html = html.replace(/^##\s+(.+)$/gm, "<h2>$1</h2>");
  html = html.replace(/^#\s+(.+)$/gm, "<h1>$1</h1>");

  html = html.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  html = html.replace(/\*(.+?)\*/g, "<em>$1</em>");

  html = html.replace(/^\s*\d+\.\s+(.+)$/gm, "<li class=\"ordered\">$1</li>");
  html = html.replace(/(?:<li class=\"ordered\">[\s\S]*?<\/li>\n?)+/g, (chunk) => {
    return `<ol>${chunk.replace(/ class=\"ordered\"/g, "")}</ol>`;
  });

  html = html.replace(/^\s*[-*]\s+(.+)$/gm, "<li>$1</li>");
  html = html.replace(/(?:<li>[\s\S]*?<\/li>\n?)+/g, (chunk) => {
    if (chunk.includes("<ol>")) return chunk;
    return `<ul>${chunk}</ul>`;
  });

  html = replaceCitations(html);
  html = codeBlocks.restore(markdownLinks.restore(inlineCode.restore(html)));

  return renderParagraphs(html);
}
