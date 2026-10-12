"""Settings: defaults < ~/.finch/config.json < environment variables < CLI flags.

Nothing machine-specific is hardcoded. The Ollama endpoint comes from OLLAMA_HOST
(bind addresses such as 0.0.0.0 are rewritten to loopback for the client side).
"""
import json
import os
import shutil
from dataclasses import dataclass, field, asdict

FINCH_HOME = os.path.expanduser(os.environ.get("FINCH_HOME") or "~/.finch")
CONFIG_FILE = os.path.join(FINCH_HOME, "config.json")
SESSIONS_DIR = os.path.join(FINCH_HOME, "sessions")
MEMORY_FILE = os.path.join(FINCH_HOME, "memory.json")
SKILLS_DIR_HOME = os.path.join(FINCH_HOME, "skills")
MCP_CONFIG = os.path.join(FINCH_HOME, "mcp.json")
CACHE_DIR = os.path.join(FINCH_HOME, "cache")
HISTORY_FILE = os.path.join(FINCH_HOME, "history")


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
    api_timeout: int = 240
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
    tricks: dict = field(default_factory=dict)   # trick key -> bool (missing = on)

    def on(self, key):
        """Is this harness trick enabled? (see tricks.py)"""
        return bool(self.tricks.get(key, True))

    def set_trick(self, key, value):
        self.tricks[key] = bool(value)

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
        s.python = shutil.which("python3") or shutil.which("python") or "python3"
    return s


PERSISTED = ("model", "num_ctx", "num_predict", "think", "offline", "host", "backend", "openai_base_url",
             "temperature", "keep_alive", "bash_timeout", "theme", "soul", "tricks", "verbose", "lang_hint")


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


def atomic_write_json(path, obj):
    """Write to .tmp then os.replace: a kill mid-write never leaves a truncated file."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def ensure_dirs():
    for d in (FINCH_HOME, SESSIONS_DIR, SKILLS_DIR_HOME, CACHE_DIR):
        os.makedirs(d, exist_ok=True)
