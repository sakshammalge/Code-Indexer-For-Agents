"""Minimal JSON-RPC-over-stdio MCP client used by the tests (independent of the SDK version)."""

from __future__ import annotations

import json
import subprocess
import sys


class StdioClient:
    def __init__(self, args: list[str], cwd: str | None = None):
        self.proc = subprocess.Popen(args, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True, bufsize=1)
        self._id = 0

    def request(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        msg = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}}
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError("server closed stdout: " + self.proc.stderr.read())
            resp = json.loads(line)
            if resp.get("id") == self._id:
                if "error" in resp:
                    raise RuntimeError(resp["error"])
                return resp["result"]

    def notify(self, method: str, params: dict | None = None) -> None:
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method, "params": params or {}}) + "\n")
        self.proc.stdin.flush()

    def initialize(self) -> dict:
        res = self.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                          "clientInfo": {"name": "ctxindex-test", "version": "0"}})
        self.notify("notifications/initialized")
        return res

    def call(self, tool: str, **arguments) -> str:
        res = self.request("tools/call", {"name": tool, "arguments": arguments})
        assert not res.get("isError"), res
        return "".join(c.get("text", "") for c in res["content"])

    def close(self) -> None:
        self.proc.stdin.close()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


if __name__ == "__main__":  # manual smoke test: python tests/mcp_client.py <project>
    c = StdioClient([sys.executable, "-m", "ctxindex", "serve", "--root", sys.argv[1]])
    print(c.initialize()["serverInfo"])
    print([t["name"] for t in c.request("tools/list")["tools"]])
    print(c.call("find_symbol", name=sys.argv[2] if len(sys.argv) > 2 else "main"))
    c.close()
