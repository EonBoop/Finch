"""Model backends: Ollama native /api/chat and any OpenAI-compatible local server.

Tricks carried over from Mingbird:
- `think` and `keep_alive` are TOP-LEVEL /api/chat fields (inside options they are
  silently dropped).
- think is tri-state; if a non-thinking model rejects the field with HTTP 400 we
  retry once without it.
- inline <think>...</think> in content is split into the thinking channel, even
  when the tags are torn across stream chunks.
- one request per turn (no double POST), streaming callbacks for content/thinking.
"""
import json
import re
import threading
import urllib.error
import urllib.request


class LLMError(Exception):
    def __init__(self, msg, code=None, body=""):
        super().__init__(msg)
        self.code = code
        self.body = body


class Cancelled(Exception):
    pass


class InlineThinkSplitter:
    """Streams content and routes text inside <think>...</think> to the think channel.
    Handles tags split across chunks by holding back a possible partial tag."""
    OPEN, CLOSE = "<think>", "</think>"

    def __init__(self, on_token, on_think):
        self.on_token, self.on_think = on_token, on_think
        self.buf = ""
        self.inside = False
        self.content, self.thinking = [], []

    def _emit(self, text, think):
        if not text:
            return
        if think:
            self.thinking.append(text)
            if self.on_think:
                self.on_think(text)
        else:
            self.content.append(text)
            if self.on_token:
                self.on_token(text)

    def feed(self, chunk):
        self.buf += chunk
        while self.buf:
            tag = self.CLOSE if self.inside else self.OPEN
            idx = self.buf.find(tag)
            if idx >= 0:
                self._emit(self.buf[:idx], self.inside)
                self.buf = self.buf[idx + len(tag):]
                self.inside = not self.inside
                continue
            # keep a tail that could be the start of a tag
            keep = 0
            for k in range(min(len(tag) - 1, len(self.buf)), 0, -1):
                if tag.startswith(self.buf[-k:]):
                    keep = k
                    break
            self._emit(self.buf[:len(self.buf) - keep], self.inside)
            self.buf = self.buf[len(self.buf) - keep:]
            break

    def flush(self):
        self._emit(self.buf, self.inside)
        self.buf = ""


def _post(url, payload, timeout, headers=None):
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=h)
    return urllib.request.urlopen(req, timeout=timeout)


def _http_error(e):
    body = ""
    try:
        body = e.read().decode("utf-8", "replace")[:500]
    except Exception:
        pass
    return LLMError(f"HTTP {e.code}: {body or e.reason}", code=e.code, body=body)


class OllamaBackend:
    def __init__(self, settings):
        self.s = settings
        self._caps = {}

    @property
    def host(self):
        return self.s.host

    def list_models(self):
        with urllib.request.urlopen(f"{self.host}/api/tags", timeout=5) as r:
            data = json.loads(r.read())
        return [m["name"] for m in data.get("models", [])]

    def show(self, model):
        if model in self._caps:
            return self._caps[model]
        info = {}
        try:
            with _post(f"{self.host}/api/show", {"model": model}, 10) as r:
                d = json.loads(r.read())
            info["capabilities"] = d.get("capabilities") or []
            info["parameter_size"] = (d.get("details") or {}).get("parameter_size", "")
            ctx = None
            for k, v in (d.get("model_info") or {}).items():
                if k.endswith(".context_length"):
                    ctx = v
            info["context_length"] = ctx
        except Exception:
            pass
        self._caps[model] = info
        return info

    def version(self):
        with urllib.request.urlopen(f"{self.host}/api/version", timeout=3) as r:
            return json.loads(r.read()).get("version", "?")

    def chat(self, model, messages, tools=None, stream=True, on_token=None, on_think=None,
             num_ctx=None, cancel=None):
        opts = {"num_ctx": num_ctx or self.s.num_ctx, "num_predict": self.s.num_predict}
        if self.s.temperature is not None:
            opts["temperature"] = self.s.temperature
        payload = {"model": model, "messages": messages, "tools": tools or [],
                   "stream": stream, "options": opts, "keep_alive": self.s.keep_alive}
        if self.s.think is not None:
            payload["think"] = bool(self.s.think)
        url = f"{self.host}/api/chat"
        timeout = self.s.api_timeout if stream else 900
        try:
            resp = _post(url, payload, timeout)
        except urllib.error.HTTPError as e:
            err = _http_error(e)
            if "think" in payload and e.code == 400:
                payload.pop("think")       # non-thinking model rejects the field: retry once
                try:
                    resp = _post(url, payload, timeout)
                except urllib.error.HTTPError as e2:
                    raise _http_error(e2)
            else:
                raise err
        except urllib.error.URLError as e:
            raise LLMError(f"cannot reach Ollama at {self.host}: {e.reason}")
        if not stream:
            with resp:
                d = json.loads(resp.read())
            msg = d.get("message") or {}
            c = msg.get("content") or ""
            if "<think>" in c:
                sp = InlineThinkSplitter(None, None)
                sp.feed(c); sp.flush()
                msg["content"] = "".join(sp.content)
                msg["thinking"] = (msg.get("thinking") or "") + "".join(sp.thinking)
            return {"message": msg, "prompt_eval_count": d.get("prompt_eval_count"),
                    "eval_count": d.get("eval_count"), "done_reason": d.get("done_reason")}
        splitter = InlineThinkSplitter(on_token, on_think)
        thinks, tool_calls = [], []
        prompt_ev = eval_ev = done_reason = None
        watchdog = None
        if cancel is not None:
            # closing the response from another thread unblocks a stalled read
            def _watch():
                cancel.wait()
                try:
                    resp.close()
                except Exception:
                    pass
            watchdog = threading.Thread(target=_watch, daemon=True)
            watchdog.start()
        try:
            for line in resp:
                if cancel is not None and cancel.is_set():
                    raise Cancelled()
                line = line.strip()
                if not line:
                    continue
                try:
                    chunk = json.loads(line)
                except Exception:
                    continue
                if chunk.get("error"):
                    raise LLMError(str(chunk["error"]), code=500, body=str(chunk["error"]))
                m = chunk.get("message") or {}
                if m.get("content"):
                    splitter.feed(m["content"])
                th = m.get("thinking") or m.get("reasoning_content")
                if th:
                    thinks.append(th)
                    if on_think:
                        on_think(th)
                if m.get("tool_calls"):
                    tool_calls.extend(m["tool_calls"])
                if chunk.get("done"):
                    prompt_ev = chunk.get("prompt_eval_count")
                    eval_ev = chunk.get("eval_count")
                    done_reason = chunk.get("done_reason")
                    break
        except (ValueError, OSError) as e:
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            raise LLMError(f"stream error: {e}")
        finally:
            try:
                resp.close()
            except Exception:
                pass
        splitter.flush()
        msg = {"role": "assistant", "content": "".join(splitter.content)}
        think_txt = "".join(thinks) + "".join(splitter.thinking)
        if think_txt:
            msg["thinking"] = think_txt
        if tool_calls:
            msg["tool_calls"] = tool_calls
        return {"message": msg, "prompt_eval_count": prompt_ev, "eval_count": eval_ev,
                "done_reason": done_reason}


class OpenAIBackend:
    """LM Studio / llama-server / vLLM / Jan ... via /v1/chat/completions (SSE)."""

    def __init__(self, settings):
        self.s = settings

    @property
    def host(self):
        return self.s.openai_base_url

    def _headers(self):
        return {"Authorization": f"Bearer {self.s.openai_api_key}"} if self.s.openai_api_key else {}

    def list_models(self):
        req = urllib.request.Request(f"{self.host.rstrip('/')}/models", headers=self._headers())
        with urllib.request.urlopen(req, timeout=5) as r:
            return [m["id"] for m in json.loads(r.read()).get("data", [])]

    def show(self, model):
        return {}

    def version(self):
        return "openai-compatible"

    @staticmethod
    def _to_openai(messages):
        out, pending_ids = [], []
        for m in messages:
            role = m.get("role")
            if role == "assistant" and m.get("tool_calls"):
                tcs = []
                for i, tc in enumerate(m["tool_calls"]):
                    fn = tc.get("function", {})
                    tid = tc.get("id") or f"call_{len(out)}_{i}"
                    pending_ids.append(tid)
                    args = fn.get("arguments", {})
                    tcs.append({"id": tid, "type": "function", "function": {
                        "name": fn.get("name", ""),
                        "arguments": args if isinstance(args, str) else json.dumps(args)}})
                out.append({"role": "assistant", "content": m.get("content") or "", "tool_calls": tcs})
            elif role == "tool":
                tid = pending_ids.pop(0) if pending_ids else "call_orphan"
                out.append({"role": "tool", "tool_call_id": tid, "content": m.get("content", "")})
            else:
                mm = {"role": role, "content": m.get("content", "")}
                if m.get("images"):
                    parts = [{"type": "text", "text": mm["content"]}]
                    for b in m["images"]:
                        parts.append({"type": "image_url",
                                      "image_url": {"url": "data:image/png;base64," + b}})
                    mm["content"] = parts
                out.append(mm)
        return out

    def chat(self, model, messages, tools=None, stream=True, on_token=None, on_think=None,
             num_ctx=None, cancel=None):
        payload = {"model": model, "messages": self._to_openai(messages), "stream": True,
                   "max_tokens": self.s.num_predict}
        if tools:
            payload["tools"] = tools
        if self.s.temperature is not None:
            payload["temperature"] = self.s.temperature
        try:
            resp = _post(f"{self.host.rstrip('/')}/chat/completions", payload,
                         self.s.api_timeout, self._headers())
        except urllib.error.HTTPError as e:
            raise _http_error(e)
        except urllib.error.URLError as e:
            raise LLMError(f"cannot reach {self.host}: {e.reason}")
        splitter = InlineThinkSplitter(on_token, on_think)
        thinks, calls = [], {}
        usage, finish = {}, None
        try:
            for raw in resp:
                if cancel is not None and cancel.is_set():
                    raise Cancelled()
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    ch = json.loads(data)
                except Exception:
                    continue
                if ch.get("usage"):
                    usage = ch["usage"]
                for c in ch.get("choices") or []:
                    d = c.get("delta") or {}
                    if d.get("content"):
                        splitter.feed(d["content"])
                    rc = d.get("reasoning_content") or d.get("reasoning")
                    if rc:
                        thinks.append(rc)
                        if on_think:
                            on_think(rc)
                    for tc in d.get("tool_calls") or []:
                        slot = calls.setdefault(tc.get("index", 0), {"name": "", "args": ""})
                        fn = tc.get("function") or {}
                        slot["name"] += fn.get("name") or ""
                        slot["args"] += fn.get("arguments") or ""
                    if c.get("finish_reason"):
                        finish = c["finish_reason"]
        finally:
            resp.close()
        splitter.flush()
        msg = {"role": "assistant", "content": "".join(splitter.content)}
        t = "".join(thinks) + "".join(splitter.thinking)
        if t:
            msg["thinking"] = t
        if calls:
            tcs = []
            for _, v in sorted(calls.items()):
                try:
                    a = json.loads(v["args"]) if v["args"].strip() else {}
                except Exception:
                    a = v["args"]
                tcs.append({"function": {"name": v["name"], "arguments": a}})
            msg["tool_calls"] = tcs
        return {"message": msg, "prompt_eval_count": usage.get("prompt_tokens"),
                "eval_count": usage.get("completion_tokens"),
                "done_reason": "length" if finish == "length" else finish}


def make_backend(settings):
    return OpenAIBackend(settings) if settings.backend == "openai" else OllamaBackend(settings)


_PARAMS_RE = re.compile(r"(\d+(?:\.\d+)?)b\b")


def model_params_b(model, info=None):
    """gemma4:e2b -> 2.0, qwen3.5:4b -> 4.0, from the tag; falls back to /api/show size."""
    m = _PARAMS_RE.search(str(model).lower())
    if m:
        return float(m.group(1))
    ps = (info or {}).get("parameter_size", "")
    m = re.match(r"([\d.]+)\s*([BM])", str(ps).upper())
    if m:
        v = float(m.group(1))
        return v / 1000 if m.group(2) == "M" else v
    return None
