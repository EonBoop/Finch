"""Settings: defaults < ~/.finch/config.json < environment variables < CLI flags.

Nothing machine-specific is hardcoded. The Ollama endpoint comes from OLLAMA_HOST
(bind addresses such as 0.0.0.0 are rewritten to loopback for the client side).
"""
import copy
import json
import os
import re
from dataclasses import dataclass, field, asdict

from . import osutil

FINCH_HOME = os.path.expanduser(os.environ.get("FINCH_HOME") or "~/.finch")
CONFIG_FILE = os.path.join(FINCH_HOME, "config.json")
SESSIONS_DIR = os.path.join(FINCH_HOME, "sessions")
MEMORY_FILE = os.path.join(FINCH_HOME, "memory.json")
SKILLS_DIR_HOME = os.path.join(FINCH_HOME, "skills")
MCP_CONFIG = os.path.join(FINCH_HOME, "mcp.json")
CACHE_DIR = os.path.join(FINCH_HOME, "cache")
HISTORY_FILE = os.path.join(FINCH_HOME, "history")
PROFILES_DIR = os.path.join(FINCH_HOME, "profiles")


def normalize_host(raw):
    """'0.0.0.0:11434' / 'http://0.0.0.0:11434' / 'OLLAMA_HOST:http://..' -> client URL."""
    h = (raw or "").strip().strip('"').strip("'")
    if h.upper().startswith("OLLAMA_HOST"):
        h = h.split(":", 1)[1].lstrip("=").strip()
    if not h:
        h = "http://127.0.0.1:11434"
    if "://" not in h:
        h = "http://" + h
    scheme, rest = h.split("://", 1)
    hostport = rest.split("/", 1)[0]
    if ":" not in hostport.rsplit("]", 1)[-1]:
        hostport += ":11434"
    host, port = hostport.rsplit(":", 1)
    if host in ("0.0.0.0", "", "[::]", "::"):
        host = "127.0.0.1"   # bind-all address is a server setting, not a destination
    return f"{scheme}://{host}:{port}"


def _tri(raw):
    """'1'/'on'/'true' -> True, '0'/'off'/'false' -> False, unset/'auto' -> None."""
    if raw is None:
        return None
    s = str(raw).strip().lower()
    if s in ("", "auto", "none", "default"):
        return None
    return s in ("1", "on", "true", "yes")


LOCKED_TOOLS = ("finish",)     # the finish gates hang off it, so it can't be switched off


def _set_member(lst, item, present):
    if present and item not in lst:
        lst.append(item)
    elif not present and item in lst:
        lst.remove(item)


@dataclass
class Settings:
    host: str = "http://127.0.0.1:11434"
    backend: str = "ollama"            # ollama | openai  (any OpenAI-compatible local server)
    openai_base_url: str = "http://127.0.0.1:1234/v1"
    openai_api_key: str = ""
    model: str = ""
    num_ctx: int = 32768
    num_predict: int = 8192            # per-call output cap (a cap, not a target)
    temperature: float = None          # None = don't send; let the model manifest decide
    think: bool = None                 # tri-state: None = model default
    keep_alive: str = "30m"            # keep the model resident through long tool chains
    bash_timeout: int = 120
    stream_timeout: int = 900          # silence allowed in a reply (Ollama sends nothing while a tool call is written)
    python: str = ""                   # interpreter for run_python / test guard
    offline: bool = False              # web tools are not even assembled into the prompt
    allow_env_mutation: bool = False
    unattended: bool = False           # no human to confirm -> deny by default
    search_backends: list = field(default_factory=list)
    lang_hint: str = "en"
    verbose: bool = False
    ablation: list = field(default_factory=list)
    theme: str = "claude"
    soul: str = ""                     # active SOUL.md persona ("" = none), see souls.py
    system_prompt: str = ""            # user's custom system prompt text
    system_prompt_mode: str = "builtin"  # builtin (text ignored) | append (after built-in) | replace (task prompt)
    tricks: dict = field(default_factory=dict)   # trick key -> bool (missing = on)
    tools_off: list = field(default_factory=list)    # built-in tool names and "mcp:<server>" turned off
    skills_off: list = field(default_factory=list)   # skill names hidden from the skills tool

    def on(self, key):
        """Is this harness trick enabled? (see tricks.py)"""
        return bool(self.tricks.get(key, True))

    def set_trick(self, key, value):
        self.tricks[key] = bool(value)

    def tool_on(self, name):
        """Is this tool allowed? MCP tools ("server.tool") follow their server's switch."""
        if name in LOCKED_TOOLS:
            return True
        if "." in name:
            return "mcp:" + name.split(".", 1)[0] not in self.tools_off
        return name not in self.tools_off

    def set_tool(self, name, value):
        _set_member(self.tools_off, name, not value and name not in LOCKED_TOOLS)

    def skill_on(self, name):
        return name not in self.skills_off

    def set_skill(self, name, value):
        _set_member(self.skills_off, name, not value)

    @property
    def unsafe(self):
        return not self.on("behaviour_tiering")

    def to_json(self):
        return asdict(self)


def load_settings():
    s = Settings()
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            data = json.load(f)
        from dataclasses import fields
        names = {f.name for f in fields(s)}
        for k, v in data.items():
            if k in names:
                setattr(s, k, v)
        if "api_timeout" in data and "stream_timeout" not in data:    # legacy key (was 240s)
            s.stream_timeout = max(s.stream_timeout, int(data["api_timeout"]))
        if data.get("unsafe"):                     # legacy key
            s.set_trick("behaviour_tiering", False)
    except Exception:
        pass
    env = os.environ
    if env.get("OLLAMA_HOST"):
        s.host = env["OLLAMA_HOST"]
    s.host = normalize_host(s.host)
    if env.get("FINCH_MODEL"):
        s.model = env["FINCH_MODEL"]
    if env.get("FINCH_CTX"):
        s.num_ctx = int(env["FINCH_CTX"])
    if env.get("FINCH_NUMPREDICT"):
        s.num_predict = int(env["FINCH_NUMPREDICT"])
    if env.get("FINCH_TEMP"):
        try:
            s.temperature = float(env["FINCH_TEMP"])
        except ValueError:
            pass
    if "FINCH_THINK" in env:
        s.think = _tri(env["FINCH_THINK"])
    if env.get("FINCH_KEEPALIVE"):
        s.keep_alive = env["FINCH_KEEPALIVE"]
    if env.get("FINCH_BASH_TIMEOUT"):
        s.bash_timeout = int(env["FINCH_BASH_TIMEOUT"])
    if env.get("FINCH_UNSAFE") == "1":
        s.set_trick("behaviour_tiering", False)
    if env.get("FINCH_ALLOW_ENV_MUTATION") == "1":
        s.allow_env_mutation = True
    if env.get("FINCH_OFFLINE") == "1":
        s.offline = True
    if env.get("FINCH_ABLATION"):
        s.ablation = [x.strip() for x in env["FINCH_ABLATION"].split(",") if x.strip()]
    if s.ablation:
        from .tricks import expand
        for k in expand(s.ablation):
            s.set_trick(k, False)
    if "FINCH_SOUL" in env:
        s.soul = env["FINCH_SOUL"]
    if env.get("FINCH_THEME"):
        s.theme = env["FINCH_THEME"]
    if not s.python:
        s.python = osutil.find_python()
    return s


PERSISTED = ("model", "num_ctx", "num_predict", "think", "offline", "host", "backend", "openai_base_url",
             "temperature", "keep_alive", "bash_timeout", "stream_timeout", "theme", "soul", "system_prompt", "system_prompt_mode", "tricks", "tools_off", "skills_off",
             "verbose", "lang_hint")


def save_settings(s, keys=PERSISTED):
    """Persist user-facing preferences changed via slash commands."""
    os.makedirs(FINCH_HOME, exist_ok=True)
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}
    for k in keys:
        data[k] = getattr(s, k)
    atomic_write_json(CONFIG_FILE, data)


# ---- profiles: named snapshots of the PERSISTED settings ----
WIN_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
PROFILE_NAME = re.compile(r"^[\w][\w.-]{0,47}$")


def profile_path(name):
    return os.path.join(PROFILES_DIR, name + ".json")


def list_profiles():
    """[{name, path, data, mtime}] sorted by name; unreadable files are skipped."""
    out = []
    if os.path.isdir(PROFILES_DIR):
        for f in sorted(os.listdir(PROFILES_DIR)):
            if f.endswith(".json"):
                p = os.path.join(PROFILES_DIR, f)
                try:
                    with open(p, encoding="utf-8") as fh:
                        data = json.load(fh)
                except Exception:
                    continue
                if isinstance(data, dict):
                    out.append({"name": f[:-5], "path": p, "data": data, "mtime": os.path.getmtime(p)})
    return out


def snapshot(s):
    return {k: copy.deepcopy(getattr(s, k)) for k in PERSISTED}


def save_profile(s, name):
    if not PROFILE_NAME.match(name or ""):
        raise ValueError("profile names are letters, digits, _ . - (max 48)")
    if name.split(".")[0].upper() in WIN_RESERVED:
        raise ValueError(f"'{name}' is a reserved file name on Windows")
    atomic_write_json(profile_path(name), snapshot(s))
    return profile_path(name)


def load_profile(name):
    try:
        with open(profile_path(name), encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        raise ValueError(f"no profile named '{name}'")
    except Exception as e:
        raise ValueError(f"profile '{name}' is unreadable: {e}")
    return {k: v for k, v in data.items() if k in PERSISTED}


def apply_profile(s, data):
    """Copy a profile's values onto the settings. Keys the profile lacks keep their current value."""
    for k, v in data.items():
        if k in PERSISTED:
            setattr(s, k, copy.deepcopy(v))
    s.host = normalize_host(s.host)


def profile_diff(s, data):
    """Settings keys where the profile differs from the current settings."""
    norm = lambda v: sorted(map(str, v)) if isinstance(v, list) else v      # toggle lists are sets
    cur = snapshot(s)
    return [k for k in PERSISTED if k in data and norm(cur.get(k)) != norm(data[k])]


def delete_profile(name):
    try:
        os.remove(profile_path(name))
        return True
    except FileNotFoundError:
        return False


def atomic_write_json(path, obj):
    """Write to .tmp then os.replace: a kill mid-write never leaves a truncated file."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    osutil.replace(tmp, path)


def ensure_dirs():
    for d in (FINCH_HOME, SESSIONS_DIR, SKILLS_DIR_HOME, CACHE_DIR, PROFILES_DIR):
        os.makedirs(d, exist_ok=True)
