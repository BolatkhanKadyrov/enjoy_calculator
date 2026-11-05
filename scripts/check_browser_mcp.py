"""Probe the installed project browser MCP over stdio; does not sign in or submit forms."""
import argparse
import json
import queue
import subprocess
import sys
import threading
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class Client:
    def __init__(self, config):
        self.messages = queue.Queue()
        self.sequence = 0
        self.log = (ROOT / ".local/browser-artifacts/mcp-stderr.log").open("w", encoding="utf-8")
        self.process = subprocess.Popen([config["command"], *config["args"]], cwd=config["cwd"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log, text=True, encoding="utf-8",
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0)
        threading.Thread(target=self.read, daemon=True).start()

    def read(self):
        for line in self.process.stdout:
            try:
                self.messages.put(json.loads(line))
            except json.JSONDecodeError:
                continue
        self.messages.put({"closed": True})

    def send(self, message):
        self.process.stdin.write(json.dumps({"jsonrpc": "2.0", **message}) + "\n")
        self.process.stdin.flush()

    def request(self, method, params):
        self.sequence += 1
        request_id = self.sequence
        self.send({"id": request_id, "method": method, "params": params})
        while True:
            item = self.messages.get(timeout=60)
            if item.get("closed"):
                raise RuntimeError("MCP process closed; see local stderr log")
            if "method" in item and "id" in item:
                if item["method"] == "roots/list":
                    self.send({"id": item["id"], "result": {"roots": [{"uri": ROOT.as_uri(), "name": "Calculator"}]}})
                elif item["method"] == "ping":
                    self.send({"id": item["id"], "result": {}})
                else:
                    self.send({"id": item["id"], "error": {"code": -32601, "message": "Unsupported client method"}})
            elif item.get("id") == request_id:
                if "error" in item:
                    raise RuntimeError(item["error"])
                return item["result"]

    def call(self, name, arguments):
        result = self.request("tools/call", {"name": name, "arguments": arguments})
        for block in result.get("content", []):
            if block.get("type") == "text":
                print(block["text"])
        if result.get("isError"):
            raise RuntimeError(f"MCP tool failed: {name}")
        return result

    def close(self):
        self.process.stdin.close()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            self.process.wait(timeout=10)
        self.log.close()


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--navigate", action="store_true")
    parser.add_argument("--screenshot", action="store_true")
    args = parser.parse_args()
    config = tomllib.loads((Path.home() / ".codex/config.toml").read_text(encoding="utf-8-sig"))["mcp_servers"]["playwright_tseh"]
    client = Client(config)
    browser_open = False
    try:
        info = client.request("initialize", {"protocolVersion": "2025-03-26", "capabilities": {"roots": {"listChanged": False}},
            "clientInfo": {"name": "tseh-browser-probe", "version": "1.0"}})
        print(json.dumps({"server": info.get("serverInfo"), "protocolVersion": info.get("protocolVersion")}, ensure_ascii=False))
        client.send({"method": "notifications/initialized"})
        catalog = client.request("tools/list", {})
        (ROOT / ".local/browser-artifacts/mcp-tools.json").write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")
        print("MCP tools:", ", ".join(tool["name"] for tool in catalog["tools"]))
        if args.navigate:
            browser_open = True
            client.call("browser_navigate", {"url": "http://127.0.0.1:8000/"})
        if args.screenshot:
            if not args.navigate:
                raise ValueError("--screenshot requires --navigate")
            client.call("browser_take_screenshot", {"type": "png", "filename": ".local/browser-artifacts/login-desktop.png", "fullPage": True, "scale": "css"})
    finally:
        if browser_open:
            try:
                client.call("browser_close", {})
            except Exception as exc:
                print("Browser close:", type(exc).__name__)
        client.close()


if __name__ == "__main__":
    main()
