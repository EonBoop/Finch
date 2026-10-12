"""Minimal MCP client (stdio, JSON-RPC 2.0, newline-delimited) with no SDK dependency.

~/.finch/mcp.json:
  {"servers": {"fs": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "."],
                      "env": {}, "keywords": ["files"], "always": false}}}

Tools are exposed FLAT as "server.tool" so the model calls them directly, but only when
the task routes to them (server name / keyword in the task, or "always": true, or the
model asks via enable_tools). Probe results are cached on disk: probing spawns processes.
"""
import json
import os
import subprocess
import threading
import time

from . import config

MANIFEST_CACHE = os.path.join(config.FINCH_HOME, "mcp_manifest_cache.json")
TTL = 300
PATH_KEYS = ("path", "filepath", "file", "dir", "folder", "filename", "directory", "source",
             "destination", "output", "input")


class StdioServer:
    def __init__(self, name, cfg):
        self.name, self.cfg = name, cfg
        self.proc = None
        self._id = 0
        self._lock = threading.Lock()

    def start(self):
        if self.proc and self.proc.poll() is None:
            return
        env = dict(os.environ)
        env.update(self.cfg.get("env") or {})
        self.proc = subprocess.Popen([self.cfg["command"], *self.cfg.get("args", [])],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, env=env, cwd=self.cfg.get("cwd"),
                                     text=True, encoding="utf-8", bufsize=1, start_new_session=True)
        self._rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                 "clientInfo": {"name": "finch", "version": "0.1"}})
        self._notify("notifications/initialized")

    def _notify(self, method, params=None):
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method, "params": params or {}}) + "\n")
        self.proc.stdin.flush()

    def _rpc(self, method, params, timeout=30):
        with self._lock:
            self._id += 1
            rid = self._id
            self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": rid, "method": method,
                                              "params": params}) + "\n")
            self.proc.stdin.flush()
            box = {}

            def _read():
                while True:
                    line = self.proc.stdout.readline()
                    if not line:
                        box["err"] = "server closed the connection"
                        return
                    try:
                        msg = json.loads(line)
                    except Exception:
                        continue
                    if msg.get("id") == rid:
                        box["msg"] = msg
                        return
            t = threading.Thread(target=_read, daemon=True)
            t.start()
            t.join(timeout)
            if t.is_alive():
                self.stop()
                raise TimeoutError(f"MCP {self.name}.{method} timed out after {timeout}s")
            if "err" in box:
                raise RuntimeError(box["err"])
            msg = box["msg"]
            if "error" in msg:
                raise RuntimeError(msg["error"].get("message", str(msg["error"])))
            return msg.get("result") or {}

    def list_tools(self):
        self.start()
        return self._rpc("tools/list", {}).get("tools", [])

    def call(self, tool, args):
        self.start()
        res = self._rpc("tools/call", {"name": tool, "arguments": args}, timeout=120)
        parts = []
        for c in res.get("content", []):
            if c.get("type") == "text":
                parts.append(c.get("text", ""))
            else:
                parts.append(f"[{c.get('type')} content]")
        out = "\n".join(parts) or json.dumps(res)[:2000]
        return ("[MCP tool error] " if res.get("isError") else "") + out[:8000]

    def stop(self):
        if self.proc:
            try:
                self.proc.kill()
            except Exception:
                pass
            self.proc = None


def _compact_schema(js):
    """Strip descriptions/titles from nested parameter schemas (prefill is precious)."""
    if isinstance(js, dict):
        return {k: _compact_schema(v) for k, v in js.items()
                if k not in ("description", "title", "$schema", "examples", "default")}
    if isinstance(js, list):
        return [_compact_schema(x) for x in js]
    return js


class MCPManager:
    def __init__(self, settings):
        self.s = settings
        self.servers = {}
        self._manifest = None
        self._t = 0

    def config(self):
        try:
            with open(config.MCP_CONFIG, encoding="utf-8") as f:
                data = json.load(f)
            return data.get("servers", data)
        except Exception:
            return {}

    def server(self, name):
        cfg = self.config().get(name)
        if not cfg or "command" not in cfg:
            return None
        if name not in self.servers:
            self.servers[name] = StdioServer(name, cfg)
        return self.servers[name]

    def manifest(self, force=False):
        """{server: [{"name", "description", "inputSchema"}]} with memory+disk cache."""
        cfgs = self.config()
        if not cfgs:
            return {}
        if self._manifest is not None and not force and time.time() - self._t < TTL:
            return self._manifest
        if not force:
            try:
                with open(MANIFEST_CACHE, encoding="utf-8") as f:
                    d = json.load(f)
                if time.time() - d.get("t", 0) < 86400 and set(d.get("data", {})) == set(cfgs):
                    self._manifest, self._t = d["data"], time.time()
                    return self._manifest
            except Exception:
                pass
        data = {}
        for name in cfgs:
            if self.s.offline and "url" in cfgs[name]:
                continue
            srv = self.server(name)
            if not srv:
                continue
            try:
                data[name] = [{"name": t["name"], "description": (t.get("description") or "")[:160],
                               "inputSchema": _compact_schema(t.get("inputSchema") or {"type": "object"})}
                              for t in srv.list_tools()]
            except Exception as e:
                data[name] = []
                data.setdefault("_errors", {})[name] = str(e)[:200]
        self._manifest, self._t = data, time.time()
        try:
            config.atomic_write_json(MANIFEST_CACHE, {"t": self._t, "data": data})
        except Exception:
            pass
        return data

    def is_tool(self, flat):
        server, _, tool = flat.partition(".")
        return any(t["name"] == tool for t in self.manifest().get(server, []) or [])

    def tool_defs(self, task_text="", extra=()):
        """Flat tool definitions for servers relevant to this task."""
        cfgs = self.config()
        if not cfgs:
            return []
        low = (task_text or "").lower()
        out = []
        for server, tools in self.manifest().items():
            if server.startswith("_") or not isinstance(tools, list):
                continue
            cfg = cfgs.get(server, {})
            kws = [server.lower(), *[k.lower() for k in cfg.get("keywords", [])]]
            routed = cfg.get("always") or "mcp" in low or any(k in low for k in kws)
            for t in tools:
                flat = f"{server}.{t['name']}"
                if routed or flat in extra or server in extra:
                    out.append({"type": "function", "function": {
                        "name": flat, "description": t["description"] or flat,
                        "parameters": t["inputSchema"]}})
        return out

    def _resolve_paths(self, args, workdir):
        out = {}
        for k, v in (args or {}).items():
            if isinstance(v, str) and k.lower() in PATH_KEYS and v and not os.path.isabs(v) and "://" not in v:
                out[k] = os.path.abspath(os.path.join(workdir, v))
            else:
                out[k] = v
        return out

    def call(self, server, tool, args, workdir):
        srv = self.server(server)
        if not srv:
            return f"[MCP server '{server}' not configured. Configured: {', '.join(self.config()) or 'none'}]"
        try:
            res = srv.call(tool, self._resolve_paths(args, workdir))
        except Exception as e:
            return f"[mcp_call error: {e}]"
        if any(k in res.lower() for k in ("access denied", "outside allowed", "not in allowed", "permission denied")):
            res += ("\n[hint: that is the MCP server's own sandbox. To read/write files in the agent's "
                    "workdir use the built-in create_file/read_file/edit_file/list_dir.]")
        return res

    def call_flat(self, flat, args, workdir):
        server, _, tool = flat.partition(".")
        return self.call(server, tool, args, workdir)

    def close(self):
        for s in self.servers.values():
            s.stop()
