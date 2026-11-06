"""Stand-in ACP v1 agent over stdio (newline JSON-RPC) for AcpDriver tests; no model, no network.

Env:
  FAKE_ACP_LOG        path receiving every inbound message as a JSON line
  FAKE_ACP_SILENT     never answer `initialize`
  FAKE_ACP_NO_RESUME  answer `session/resume` with method-not-found (forces the `session/load` fallback)
Prompt text steers the script: "permission" asks the client for a tool permission and reports the
chosen option; "slow" streams one chunk and waits for `session/cancel`; "quota" errors with a 429;
anything else streams two chunks and ends.
"""
import json
import os
import sys
import time


def _send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _chunk(sid, text):
    _send({"jsonrpc": "2.0", "method": "session/update", "params": {
        "sessionId": sid, "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}}}})


def main():
    log = os.environ.get("FAKE_ACP_LOG")
    sessions = 0
    pending = None
    for line in sys.stdin:
        msg = json.loads(line)
        if log:
            with open(log, "a", encoding="utf-8") as f:
                f.write(json.dumps(msg) + "\n")
        method, rid = msg.get("method"), msg.get("id")
        if method is None:
            if rid == "perm-1" and pending:
                outcome = msg["result"]["outcome"]
                _chunk(pending[1], "permission:" + str(outcome.get("optionId") or outcome["outcome"]))
                _send({"jsonrpc": "2.0", "id": pending[0], "result": {"stopReason": "end_turn"}})
                pending = None
        elif method == "initialize":
            if os.environ.get("FAKE_ACP_SILENT"):
                time.sleep(60)
                return
            _send({"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": 1, "agentCapabilities": {
                "loadSession": True, "sessionCapabilities": {"resume": {}, "close": {}}}}})
        elif method == "session/new":
            sessions += 1
            _send({"jsonrpc": "2.0", "id": rid, "result": {"sessionId": f"sess-{sessions}"}})
        elif method == "session/resume":
            if os.environ.get("FAKE_ACP_NO_RESUME"):
                _send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "method not found"}})
            else:
                _send({"jsonrpc": "2.0", "id": rid, "result": {}})
        elif method == "session/load":
            _chunk(msg["params"]["sessionId"], "replayed history")
            _send({"jsonrpc": "2.0", "id": rid, "result": None})
        elif method == "session/close":
            _send({"jsonrpc": "2.0", "id": rid, "result": {}})
        elif method == "session/prompt":
            sid = msg["params"]["sessionId"]
            text = msg["params"]["prompt"][0]["text"]
            if "permission" in text:
                pending = (rid, sid)
                _send({"jsonrpc": "2.0", "id": "perm-1", "method": "session/request_permission", "params": {
                    "sessionId": sid, "toolCall": {"toolCallId": "t1", "title": "write file", "kind": "edit"},
                    "options": [
                        {"optionId": "allow-once", "name": "Allow", "kind": "allow_once"},
                        {"optionId": "reject-once", "name": "Reject", "kind": "reject_once"}]}})
            elif "slow" in text:
                pending = (rid, sid, "slow")
                _chunk(sid, "working")
            elif "quota" in text:
                _send({"jsonrpc": "2.0", "id": rid, "error": {"code": 429, "message": "429 free-usage-exhausted"}})
            else:
                _chunk(sid, "hello ")
                _chunk(sid, "world")
                _send({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "end_turn"}})
        elif method == "session/cancel":
            if pending and len(pending) == 3:
                _send({"jsonrpc": "2.0", "id": pending[0], "result": {"stopReason": "cancelled"}})
                pending = None


if __name__ == "__main__":
    main()
