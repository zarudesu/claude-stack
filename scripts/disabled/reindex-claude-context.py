#!/usr/bin/env python3
"""Index projects into Milvus via the claude-context MCP server (stdio JSON-RPC).

Needed when Claude Code session tools are unavailable (e.g. after a Milvus
rebuild — MCP connects at session start) or for batch reindexing outside a session.

Usage:
  python3 reindex-claude-context.py ~/Projects/foo ~/Projects/bar [--force]
  python3 reindex-claude-context.py --status ~/Projects/foo

Server env is read from ~/.claude.json (mcpServers.claude-context.env); override
the path with CLAUDE_CONTEXT_MCP_CONFIG. Falls back to local defaults. Requires: Milvus healthy, Ollama running.
"""
import argparse
import json
import os
import subprocess
import sys
import threading
import time

DEFAULT_ENV = {
    "EMBEDDING_PROVIDER": "Ollama",
    "EMBEDDING_MODEL": "qwen3-embedding:0.6b",
    "EMBEDDING_DIMENSION": "1024",
    "OLLAMA_HOST": "http://127.0.0.1:11434",
    "MILVUS_ADDRESS": "127.0.0.1:19530",
    "MILVUS_TOKEN": "local",
}
CONFIG = os.path.expanduser(
    os.environ.get("CLAUDE_CONTEXT_MCP_CONFIG", "~/.claude.json")
)
# Pinned on purpose: a different server version can change embedding defaults,
# and the vector dimension is baked into the Milvus collection.
MCP_PACKAGE = os.environ.get(
    "CLAUDE_CONTEXT_MCP_PACKAGE", "@zilliz/claude-context-mcp@0.1.15"
)


def server_env():
    env = dict(os.environ)
    merged = dict(DEFAULT_ENV)
    try:
        with open(CONFIG) as f:
            merged.update(json.load(f)["mcpServers"]["claude-context"].get("env", {}))
    except Exception:
        pass
    env.update(merged)
    return env


class McpClient:
    def __init__(self):
        self.proc = subprocess.Popen(
            ["npx", "-y", MCP_PACKAGE],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=open(os.path.expanduser("~/.claude/reindex-claude-context.err"), "ab"),
            env=server_env(), text=True, bufsize=1,
        )
        self.next_id = 1
        self.send("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "reindex-script", "version": "1.0"},
        })
        self.read_result()
        self.notify("notifications/initialized")

    def send(self, method, params=None):
        msg = {"jsonrpc": "2.0", "id": self.next_id, "method": method}
        if params is not None:
            msg["params"] = params
        self.next_id += 1
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def notify(self, method, params=None):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def read_result(self, timeout=120):
        deadline = time.time() + timeout
        box = {}

        def reader():
            while time.time() < deadline:
                line = self.proc.stdout.readline()
                if not line:
                    return
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if "id" in msg and ("result" in msg or "error" in msg):
                    box["msg"] = msg
                    return

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        t.join(timeout)
        return box.get("msg")

    def call_tool(self, name, args, timeout=300):
        self.send("tools/call", {"name": name, "arguments": args})
        msg = self.read_result(timeout)
        if msg is None:
            return "<timeout waiting for server reply>"
        if "error" in msg:
            return f"<error> {msg['error']}"
        parts = msg["result"].get("content", [])
        return "\n".join(p.get("text", "") for p in parts if p.get("type") == "text")

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.terminate()
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--force", action="store_true", help="drop and rebuild existing index")
    ap.add_argument("--status", action="store_true", help="only print indexing status")
    ap.add_argument("--search", metavar="QUERY", help="run a semantic search instead of indexing")
    ap.add_argument("--clear", action="store_true", help="drop the index/collection (fix for Milvus 'index duplicates')")
    args = ap.parse_args()

    paths = [os.path.realpath(os.path.expanduser(p)) for p in args.paths]
    for p in paths:
        if not os.path.isdir(p):
            sys.exit(f"not a directory: {p}")

    client = McpClient()
    try:
        for p in paths:
            if args.clear:
                print(f"=== clearing index of {p}")
                print(client.call_tool("clear_index", {"path": p}), flush=True)
                continue
            if args.search:
                print(f"=== search '{args.search}' in {p}")
                print(client.call_tool("search_code", {"path": p, "query": args.search, "limit": 5}), flush=True)
                continue
            if args.status:
                print(f"=== {p}\n{client.call_tool('get_indexing_status', {'path': p})}", flush=True)
                continue
            print(f"=== indexing {p}", flush=True)
            print(client.call_tool("index_codebase", {"path": p, "force": args.force}), flush=True)
            # index_codebase kicks off async indexing inside the server process —
            # keep it alive and poll until this path reports completion.
            prev, stale = "", 0
            for _ in range(480):  # hard cap: 2h per project
                time.sleep(15)
                status = client.call_tool("get_indexing_status", {"path": p})
                line = status.replace("\n", " ")[:220]
                print(f"    {time.strftime('%H:%M:%S')} {line}", flush=True)
                low = status.lower()
                if any(k in low for k in ("fully indexed", "completed", "complete", "failed", "error", "not found")):
                    break
                # server can hang with a frozen status (seen: "100.0% being indexed"
                # with a stale Last-updated for 1.5h) — don't let it block the queue
                stale = stale + 1 if status == prev else 0
                prev = status
                if stale >= 12:  # ~3 min without any change
                    print(f"    !! status frozen for 3 min — skipping {p}, retry later with --force", flush=True)
                    break
    finally:
        client.close()


if __name__ == "__main__":
    main()
