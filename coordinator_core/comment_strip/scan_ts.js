

const path = require("path");
const readline = require("readline");

function scanText(ts, variant, text) {
  const langVariant = variant === "jsx" ? ts.LanguageVariant.JSX : ts.LanguageVariant.Standard;
  const scanner = ts.createScanner(ts.ScriptTarget.Latest,  false, langVariant, text);
  scanner.setText(text);

  const out = [];
  let tok = scanner.scan();
  while (tok !== ts.SyntaxKind.EndOfFileToken) {
    if (tok === ts.SyntaxKind.SingleLineCommentTrivia || tok === ts.SyntaxKind.MultiLineCommentTrivia) {
      const start = scanner.getTokenStart ? scanner.getTokenStart() : scanner.getStartPos() - scanner.getTokenText().length;
      const tokenText = scanner.getTokenText();
      const end = start + tokenText.length;
      out.push({
        start,
        end,
        text: tokenText,
        kind: tok === ts.SyntaxKind.SingleLineCommentTrivia ? "line" : "block",
      });
    }
    tok = scanner.scan();
  }
  return out;
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
      const spans = scanText(ts, req.variant || "standard", req.text);
      process.stdout.write(JSON.stringify({ id: req.id, spans }) + "\n");
    } catch (e) {
      process.stdout.write(JSON.stringify({ id: req.id, error: String(e && e.message || e) }) + "\n");
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
