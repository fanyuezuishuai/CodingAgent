(function installTraceCoderMarkdown(global) {
  "use strict";

  function escapeHtml(value) {
    return String(value)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");
  }

  function safeLinkTarget(value) {
    const compact = value.trim().replace(/[\u0000-\u001f\u007f\s]+/g, "");
    const lowered = compact.toLowerCase();
    if (lowered.startsWith("javascript:") || lowered.startsWith("data:") || lowered.startsWith("vbscript:")) {
      return null;
    }
    if (/^(https?:\/\/|mailto:|\/|\.\/|\.\.\/|#)/i.test(compact)) return compact;
    return null;
  }

  function renderInline(source) {
    const tokens = [];
    const token = (html) => {
      const marker = `\uE000${tokens.length}\uE001`;
      tokens.push(html);
      return marker;
    };

    let prepared = String(source).replace(/`([^`\n]+)`/g, (_match, code) => (
      token(`<code>${escapeHtml(code)}</code>`)
    ));
    prepared = prepared.replace(/\[([^\]\n]+)\]\(([^)\s]+)\)/g, (match, label, target) => {
      const safeTarget = safeLinkTarget(target);
      if (!safeTarget) return `${label} (${target})`;
      return token(`<a href="${escapeHtml(safeTarget)}" rel="noopener noreferrer">${escapeHtml(label)}</a>`);
    });

    let html = escapeHtml(prepared)
      .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
      .replace(/__([^_\n]+)__/g, "<strong>$1</strong>")
      .replace(/~~([^~\n]+)~~/g, "<del>$1</del>")
      .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
    html = html.replace(/\uE000(\d+)\uE001/g, (_match, index) => tokens[Number(index)] || "");
    return html;
  }

  function renderMarkdown(source) {
    const lines = String(source || "").replace(/\r\n?/g, "\n").split("\n");
    const output = [];
    let paragraph = [];
    let listType = null;
    let codeLanguage = "";
    let codeLines = null;

    const closeParagraph = () => {
      if (!paragraph.length) return;
      output.push(`<p>${renderInline(paragraph.join(" "))}</p>`);
      paragraph = [];
    };
    const closeList = () => {
      if (!listType) return;
      output.push(`</${listType}>`);
      listType = null;
    };
    const closeBlocks = () => {
      closeParagraph();
      closeList();
    };

    for (const line of lines) {
      if (codeLines !== null) {
        if (/^\s*```/.test(line)) {
          const languageClass = codeLanguage ? ` class="language-${codeLanguage}"` : "";
          output.push(`<pre><code${languageClass}>${escapeHtml(codeLines.join("\n"))}</code></pre>`);
          codeLines = null;
          codeLanguage = "";
        } else {
          codeLines.push(line);
        }
        continue;
      }

      const fence = line.match(/^\s*```\s*([A-Za-z0-9_-]*)\s*$/);
      if (fence) {
        closeBlocks();
        codeLanguage = fence[1].toLowerCase();
        codeLines = [];
        continue;
      }
      if (!line.trim()) {
        closeBlocks();
        continue;
      }

      const heading = line.match(/^(#{1,6})\s+(.+)$/);
      if (heading) {
        closeBlocks();
        const level = heading[1].length;
        output.push(`<h${level}>${renderInline(heading[2])}</h${level}>`);
        continue;
      }
      if (/^\s{0,3}([-*_])(?:\s*\1){2,}\s*$/.test(line)) {
        closeBlocks();
        output.push("<hr>");
        continue;
      }

      const unordered = line.match(/^\s*[-*+]\s+(.+)$/);
      const ordered = line.match(/^\s*\d+[.)]\s+(.+)$/);
      if (unordered || ordered) {
        closeParagraph();
        const nextType = ordered ? "ol" : "ul";
        if (listType !== nextType) {
          closeList();
          output.push(`<${nextType}>`);
          listType = nextType;
        }
        output.push(`<li>${renderInline((unordered || ordered)[1])}</li>`);
        continue;
      }

      const quote = line.match(/^\s*>\s?(.*)$/);
      if (quote) {
        closeBlocks();
        output.push(`<blockquote>${renderInline(quote[1])}</blockquote>`);
        continue;
      }
      paragraph.push(line.trim());
    }

    if (codeLines !== null) {
      const languageClass = codeLanguage ? ` class="language-${codeLanguage}"` : "";
      output.push(`<pre><code${languageClass}>${escapeHtml(codeLines.join("\n"))}</code></pre>`);
    } else {
      closeBlocks();
    }
    return output.join("\n");
  }

  global.TraceCoderMarkdown = Object.freeze({ render: renderMarkdown });
}(globalThis));
