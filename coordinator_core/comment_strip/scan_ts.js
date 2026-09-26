// AST-based comment scanning and code-equivalence proof for TS/JS/TSX/JSX.
//
// The previous implementation drove `ts.createScanner` directly, token by token, with no
// parser in the loop. That is unsound for two real cases:
//   1. A template literal containing a `${...}` substitution: after the substitution's
//      closing `}`, the raw scanner does not know it must re-enter template-literal mode
//      (only the parser calls `reScanTemplateToken()` for that) — the rest of the template
//      text gets lexed as ordinary code, and a `//` in it becomes a real (and wrong) line
//      comment, corrupting everything to the next newline.
//   2. A comment whose prose happens to contain `*/`: a real comment genuinely ends at the
//      first `*/` per the language grammar, so a raw scan of the comment alone can't tell
//      "prose that quotes it" from "actually ends here" — that ambiguity is only resolvable
//      by checking whether the surrounding file still parses the same way afterward.
//
// Fix: never scan comments as an isolated character stream. `collectComments` walks the real
// parser's AST and reads comments off each node's leading/trailing trivia (`getLeadingComment-
// Ranges`/`getTrailingCommentRanges`), which is exactly how the compiler's own emitter finds
// comments — a template literal is one leaf node, so nothing inside it is ever visited as
// trivia. `verifyEquivalent` is the load-bearing proof (ported from the EM's tscheck.js): it
// parses both texts, and requires equal parse-diagnostic counts AND an identical
// `ts.createPrinter({removeComments:true})` print. A file that doesn't parse cleanly (e.g. the
// `*/`-in-prose case) fails this outright rather than guessing intent.

const path = require("path");
const readline = require("readline");

function rangeKind(ts, kind) {
  return kind === ts.SyntaxKind.SingleLineCommentTrivia ? "line" : "block";
}

function collectComments(ts, sf) {
  const full = sf.getFullText();
  const out = [];
  const seen = new Set();
  function add(ranges) {
    if (!ranges) return;
    for (const r of ranges) {
      const key = r.pos + ":" + r.end;
      if (seen.has(key)) continue;
      seen.add(key);
      out.push({ start: r.pos, end: r.end, text: full.slice(r.pos, r.end), kind: rangeKind(ts, r.kind) });
    }
  }
  function visit(node) {
    add(ts.getLeadingCommentRanges(full, node.getFullStart()));
    add(ts.getTrailingCommentRanges(full, node.getEnd()));
    node.getChildren(sf).forEach(visit);
  }
  visit(sf);
  out.sort((a, b) => a.start - b.start);
  return out;
}

function scriptKind(ts, variant) {
  return variant === "jsx" ? ts.ScriptKind.TSX : ts.ScriptKind.TS;
}

function scanText(ts, variant, text) {
  const sf = ts.createSourceFile("f.ts", text, ts.ScriptTarget.Latest, false, scriptKind(ts, variant));
  return collectComments(ts, sf);
}

function normalizedPrint(ts, variant, text) {
  const sf = ts.createSourceFile("f.ts", text, ts.ScriptTarget.Latest, false, scriptKind(ts, variant));
  const diagCount = (sf.parseDiagnostics || []).length;
  const printer = ts.createPrinter({ removeComments: true });
  return diagCount + "|" + printer.printFile(sf);
}

function verifyEquivalent(ts, variant, origText, newText) {
  try {
    return normalizedPrint(ts, variant, origText) === normalizedPrint(ts, variant, newText);
  } catch {
    return false;
  }
}

function handleRequest(ts, req) {
  const mode = req.mode || "scan";
  if (mode === "verify") {
    return { id: req.id, equal: verifyEquivalent(ts, req.variant || "standard", req.text, req.newText) };
  }
  return { id: req.id, spans: scanText(ts, req.variant || "standard", req.text) };
}

function runBatch(ts) {
  const rl = readline.createInterface({ input: process.stdin, terminal: false });
  rl.on("line", (line) => {
    line = line.trim();
    if (!line) return;
    let req;
    try {
      req = JSON.parse(line);
    } catch (e) {
      process.stdout.write(JSON.stringify({ error: "bad-request-json: " + e.message }) + "\n");
      return;
    }
    try {
      process.stdout.write(JSON.stringify(handleRequest(ts, req)) + "\n");
    } catch (e) {
      process.stdout.write(JSON.stringify({ id: req.id, error: String((e && e.message) || e) }) + "\n");
    }
  });
}

function main() {
  const tsDir = process.argv[2];
  const ts = require(path.join(tsDir));

  if (process.argv[3] === "--batch") {
    runBatch(ts);
    return;
  }

  const file = process.argv[3];
  const variant = process.argv[4] || "standard";
  const fs = require("fs");
  const text = fs.readFileSync(file, "utf8");
  process.stdout.write(JSON.stringify(scanText(ts, variant, text)));
}
main();
