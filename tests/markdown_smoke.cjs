"use strict";

const assert = require("node:assert/strict");

require("../src/tracecoder/web_static/markdown.js");

const fence = "`".repeat(3);
const rendered = globalThis.TraceCoderMarkdown.render([
  "## Heading",
  "",
  "This is **bold** with `inline code`.",
  "",
  "- first",
  "- second",
  "",
  `${fence}cpp`,
  "int main() {}",
  fence,
  "",
  "[safe](https://example.com) [unsafe](javascript:alert(1))",
  "<script>alert(1)</script>",
].join("\n"));

assert.match(rendered, /<h2>Heading<\/h2>/);
assert.match(rendered, /<strong>bold<\/strong>/);
assert.match(rendered, /<code>inline code<\/code>/);
assert.match(rendered, /<li>first<\/li>/);
assert.match(rendered, /<pre><code class="language-cpp">int main\(\) \{\}<\/code><\/pre>/);
assert.match(rendered, /href="https:\/\/example\.com"/);
assert.doesNotMatch(rendered, /href="javascript:/i);
assert.doesNotMatch(rendered, /<script>/i);
assert.match(rendered, /&lt;script&gt;/i);

console.log("markdown smoke passed");
