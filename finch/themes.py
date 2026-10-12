"""Colour themes. Each theme defines the rich styles used by the UI and the prompt_toolkit
styles for the input line, toolbar and menus. Switch with /theme or in /settings."""
from rich.theme import Theme

_BASE = {"tool": "bold", "bold": "bold"}

THEMES = {
    "claude": {
        "desc": "warm orange accent on dark (default)",
        "rich": {"accent": "#d97757", "dim": "grey50", "ok": "green", "err": "red", "warn": "yellow",
                 "think": "italic grey50", "info": "cyan", "user": "grey70", "output": "grey62",
                 "rule": "grey30", "diff.add": "on #1f3c24", "diff.del": "on #3c1f1f",
                 "done": "strike grey50", "bullet": "white", "border": "#d97757"},
        "pt": {"accent": "#d97757", "dim": "#888888", "warn": "#d7af00", "err": "#d75f5f",
               "menu_bg": "#303030", "menu_sel": "#d97757", "fg": ""},
    },
    "ocean": {
        "desc": "cool blues and teals",
        "rich": {"accent": "#4fa3d1", "dim": "#6c7a89", "ok": "#5fd7af", "err": "#ff6b6b", "warn": "#ffd166",
                 "think": "italic #6c7a89", "info": "#8be9fd", "user": "#a9b7c6", "output": "#8fa1b3",
                 "rule": "#2c3e50", "diff.add": "on #12352f", "diff.del": "on #3b1d26",
                 "done": "strike #6c7a89", "bullet": "#cfe8ff", "border": "#4fa3d1"},
        "pt": {"accent": "#4fa3d1", "dim": "#6c7a89", "warn": "#ffd166", "err": "#ff6b6b",
               "menu_bg": "#1b2b38", "menu_sel": "#4fa3d1", "fg": ""},
    },
    "forest": {
        "desc": "greens and earth tones",
        "rich": {"accent": "#8bc34a", "dim": "#7a8b6f", "ok": "#a5d6a7", "err": "#e57373", "warn": "#ffb74d",
                 "think": "italic #7a8b6f", "info": "#80cbc4", "user": "#b5c4a8", "output": "#98a88c",
                 "rule": "#33402b", "diff.add": "on #1f3a1f", "diff.del": "on #3d2420",
                 "done": "strike #7a8b6f", "bullet": "#e8f5e9", "border": "#8bc34a"},
        "pt": {"accent": "#8bc34a", "dim": "#7a8b6f", "warn": "#ffb74d", "err": "#e57373",
               "menu_bg": "#26301f", "menu_sel": "#8bc34a", "fg": ""},
    },
    "dracula": {
        "desc": "the Dracula palette",
        "rich": {"accent": "#bd93f9", "dim": "#6272a4", "ok": "#50fa7b", "err": "#ff5555", "warn": "#f1fa8c",
                 "think": "italic #6272a4", "info": "#8be9fd", "user": "#f8f8f2", "output": "#c0c4d6",
                 "rule": "#44475a", "diff.add": "on #1e3a2a", "diff.del": "on #3f1f2a",
                 "done": "strike #6272a4", "bullet": "#ff79c6", "border": "#bd93f9"},
        "pt": {"accent": "#bd93f9", "dim": "#6272a4", "warn": "#f1fa8c", "err": "#ff5555",
               "menu_bg": "#44475a", "menu_sel": "#bd93f9", "fg": ""},
    },
    "solarized-light": {
        "desc": "for light terminal backgrounds",
        "rich": {"accent": "#cb4b16", "dim": "#93a1a1", "ok": "#859900", "err": "#dc322f", "warn": "#b58900",
                 "think": "italic #93a1a1", "info": "#268bd2", "user": "#586e75", "output": "#657b83",
                 "rule": "#d6cfb8", "diff.add": "on #e3efc9", "diff.del": "on #f6dcd6",
                 "done": "strike #93a1a1", "bullet": "#073642", "border": "#cb4b16"},
        "pt": {"accent": "#cb4b16", "dim": "#93a1a1", "warn": "#b58900", "err": "#dc322f",
               "menu_bg": "#eee8d5", "menu_sel": "#cb4b16", "fg": "#073642"},
    },
    "high-contrast": {
        "desc": "bright, bold colours for readability",
        "rich": {"accent": "bold bright_yellow", "dim": "white", "ok": "bold bright_green", "err": "bold bright_red",
                 "warn": "bold bright_yellow", "think": "italic white", "info": "bold bright_cyan",
                 "user": "bright_white", "output": "bright_white", "rule": "white",
                 "diff.add": "black on bright_green", "diff.del": "black on bright_red",
                 "done": "strike white", "bullet": "bold bright_white", "border": "bright_yellow"},
        "pt": {"accent": "ansibrightyellow bold", "dim": "ansiwhite", "warn": "ansibrightyellow",
               "err": "ansibrightred", "menu_bg": "ansiblack", "menu_sel": "ansibrightyellow", "fg": ""},
    },
    "mono": {
        "desc": "no colour, just weight",
        "rich": {"accent": "bold", "dim": "dim", "ok": "bold", "err": "bold underline", "warn": "bold",
                 "think": "italic dim", "info": "", "user": "", "output": "dim", "rule": "dim",
                 "diff.add": "reverse", "diff.del": "strike", "done": "strike dim", "bullet": "bold",
                 "border": "default"},
        "pt": {"accent": "bold", "dim": "", "warn": "bold", "err": "bold underline",
               "menu_bg": "", "menu_sel": "reverse", "fg": ""},
    },
}

def _palette(desc, accent, dim, ok, err, warn, info, text, output, rule, border, bullet,
             add_bg, del_bg, menu_bg, menu_sel, menu_sel_fg="#ffffff", fg="", glyphs=None):
    """Build a theme from a compact palette (dark-background themes unless fg/menu say otherwise)."""
    return {
        "desc": desc,
        "rich": {"accent": accent, "dim": dim, "ok": ok, "err": err, "warn": warn,
                 "think": f"italic {dim}", "info": info, "user": text, "output": output, "rule": rule,
                 "diff.add": f"on {add_bg}", "diff.del": f"on {del_bg}", "done": f"strike {dim}",
                 "bullet": bullet, "border": border},
        "pt": {"accent": accent, "dim": dim, "warn": warn, "err": err, "menu_bg": menu_bg,
               "menu_sel": menu_sel, "menu_sel_fg": menu_sel_fg, "fg": fg},
        "glyphs": glyphs or {},
    }


# ---- Hermes Agent skins (colours from NousResearch/hermes-agent hermes_cli/skin_engine.py) ----
_KAWAII_WAITING = ["(｡◕‿◕｡)", "(◕‿◕✿)", "٩(◕‿◕｡)۶", "(✿◠‿◠)", "( ˘▽˘)っ", "♪(´ε` )", "(◕ᴗ◕✿)",
                   "ヾ(＾∇＾)", "(≧◡≦)", "(★ω★)"]
_KAWAII_THINKING = ["(｡•́︿•̀｡)", "(◔_◔)", "(¬‿¬)", "( •_•)>⌐■-■", "(⌐■_■)", "(´･_･`)", "◉_◉",
                    "(°ロ°)", "( ˘⌣˘)♡", "ヽ(>∀<☆)☆", "٩(๑❛ᴗ❛๑)۶", "(⊙_⊙)", "(¬_¬)", "ಠ_ಠ"]
_HERMES_VERBS = ["pondering", "contemplating", "musing", "cogitating", "ruminating", "deliberating",
                 "mulling", "reflecting", "processing", "reasoning", "analyzing", "computing",
                 "synthesizing", "formulating", "brainstorming"]

THEMES.update({
    "hermes": _palette(
        "Hermes Agent: gold and kawaii (its default skin)",
        accent="#FFBF00", dim="#B8860B", ok="#4caf50", err="#ef5350", warn="#ffa726", info="#DAA520",
        text="#FFF8DC", output="#C0C0C0", rule="#CD7F32", border="#CD7F32", bullet="#FFD700",
        add_bg="#1f3a24", del_bg="#3c1f22", menu_bg="#1a1a2e", menu_sel="#333355",
        glyphs={"bullet": "☤", "result": "┊", "prompt": "❯", "logo": "☤", "faces": _KAWAII_THINKING,
                "waiting": _KAWAII_WAITING, "verbs": _HERMES_VERBS, "name": "Hermes"}),
    "ares": _palette(
        "Hermes skin: war god, crimson and bronze",
        accent="#DD4A3A", dim="#905151", ok="#7BC96F", err="#ef5350", warn="#ffa726", info="#C7A96B",
        text="#F1E6CF", output="#d8ccb4", rule="#A93333", border="#A93333", bullet="#C7A96B",
        add_bg="#1f3a24", del_bg="#4a1a17", menu_bg="#2A1212", menu_sel="#5C221D",
        glyphs={"bullet": "⚔", "result": "╎", "logo": "⚔", "faces": ["(⚔)", "(⛨)", "(▲)", "(⌁)", "(<>)"],
                "verbs": ["forging", "marching", "sizing the field", "holding the line", "hammering plans",
                          "tempering steel", "plotting impact", "raising the shield"], "name": "Ares"}),
    "poseidon": _palette(
        "Hermes skin: ocean god, deep blue and seafoam",
        accent="#5DB8F5", dim="#52708A", ok="#6ED7B0", err="#D94F4F", warn="#ffa726", info="#A9DFFF",
        text="#EAF7FF", output="#b9d3e6", rule="#2A6FB9", border="#2A6FB9", bullet="#A9DFFF",
        add_bg="#123a35", del_bg="#3b1d26", menu_bg="#0F2440", menu_sel="#254D73",
        glyphs={"bullet": "Ψ", "result": "│", "logo": "Ψ", "faces": ["(Ψ)", "(∿)", "(≈)", "(⌁)", "(◌)"],
                "verbs": ["charting currents", "sounding the depth", "reading foam lines", "steering the trident",
                          "tracking undertow", "plotting sea lanes", "calling the swell"], "name": "Poseidon"}),
    "charizard": _palette(
        "Hermes skin: volcanic, burnt orange and ember",
        accent="#F29C38", dim="#C58A45", ok="#6BCB77", err="#EF5350", warn="#ffa726", info="#FFD39A",
        text="#FFF0D4", output="#e2cfb0", rule="#C75B1D", border="#C75B1D", bullet="#FFD39A",
        add_bg="#1f3a24", del_bg="#4A1B07", menu_bg="#0B0503", menu_sel="#4A1B07",
        glyphs={"bullet": "✦", "result": "│", "logo": "✦", "faces": ["(✦)", "(▲)", "(◇)", "(⌁)", "(🔥)"],
                "verbs": ["banking into the draft", "measuring burn", "reading the updraft", "tracking ember fall",
                          "setting wing angle", "holding the flame core", "coiling for lift"], "name": "Charizard"}),
    "slate": _palette(
        "Hermes skin: cool blue, developer-focused",
        accent="#7eb8f6", dim="#5D6672", ok="#63D0A6", err="#F7A072", warn="#e6a855", info="#8EA8FF",
        text="#c9d1d9", output="#a9b4c0", rule="#4169e1", border="#4169e1", bullet="#7eb8f6",
        add_bg="#163a30", del_bg="#3d2622", menu_bg="#151C2F", menu_sel="#324867",
        glyphs={"bullet": "☤", "result": "┊", "logo": "☤", "faces": _KAWAII_THINKING,
                "verbs": _HERMES_VERBS, "name": "Hermes"}),
    # ---- popular editor palettes
    "nord": _palette(
        "arctic, north-bluish (Nord)",
        accent="#88C0D0", dim="#616E88", ok="#A3BE8C", err="#BF616A", warn="#EBCB8B", info="#81A1C1",
        text="#ECEFF4", output="#D8DEE9", rule="#3B4252", border="#5E81AC", bullet="#ECEFF4",
        add_bg="#2e3d33", del_bg="#3f2c31", menu_bg="#3B4252", menu_sel="#5E81AC"),
    "gruvbox": _palette(
        "retro groove, warm and earthy (Gruvbox dark)",
        accent="#fe8019", dim="#928374", ok="#b8bb26", err="#fb4934", warn="#fabd2f", info="#83a598",
        text="#ebdbb2", output="#d5c4a1", rule="#504945", border="#d65d0e", bullet="#ebdbb2",
        add_bg="#32361a", del_bg="#3c1f1e", menu_bg="#3c3836", menu_sel="#d65d0e"),
    "tokyo-night": _palette(
        "neon city at night (Tokyo Night)",
        accent="#7aa2f7", dim="#565f89", ok="#9ece6a", err="#f7768e", warn="#e0af68", info="#7dcfff",
        text="#c0caf5", output="#a9b1d6", rule="#292e42", border="#bb9af7", bullet="#c0caf5",
        add_bg="#20303b", del_bg="#37222c", menu_bg="#1f2335", menu_sel="#3d59a1"),
    "catppuccin": _palette(
        "soothing pastels (Catppuccin Mocha)",
        accent="#cba6f7", dim="#6c7086", ok="#a6e3a1", err="#f38ba8", warn="#f9e2af", info="#89dceb",
        text="#cdd6f4", output="#bac2de", rule="#313244", border="#f5c2e7", bullet="#cdd6f4",
        add_bg="#2a3a33", del_bg="#3e2735", menu_bg="#313244", menu_sel="#cba6f7", menu_sel_fg="#1e1e2e"),
    "monokai": _palette(
        "classic editor contrast (Monokai)",
        accent="#f92672", dim="#75715e", ok="#a6e22e", err="#f92672", warn="#e6db74", info="#66d9ef",
        text="#f8f8f2", output="#cfcfc2", rule="#3e3d32", border="#fd971f", bullet="#f8f8f2",
        add_bg="#2e3b1a", del_bg="#4a1a2a", menu_bg="#3e3d32", menu_sel="#f92672"),
    "rose-pine": _palette(
        "all natural pine, faux fur and a bit of soho vibes (Rosé Pine)",
        accent="#ebbcba", dim="#6e6a86", ok="#9ccfd8", err="#eb6f92", warn="#f6c177", info="#c4a7e7",
        text="#e0def4", output="#c8c5df", rule="#26233a", border="#31748f", bullet="#e0def4",
        add_bg="#1f3536", del_bg="#3a1f2b", menu_bg="#26233a", menu_sel="#403d52"),
})
for _t in THEMES.values():
    _t.setdefault("glyphs", {})
    _t["pt"].setdefault("menu_sel_fg", "#000000")

GROUPS = {
    "Finch": ["claude", "ocean", "forest", "dracula", "solarized-light", "high-contrast", "mono"],
    "Hermes Agent": ["hermes", "ares", "poseidon", "charizard", "slate"],
    "Editor palettes": ["nord", "gruvbox", "tokyo-night", "catppuccin", "monokai", "rose-pine"],
}

DEFAULT_GLYPHS = {"bullet": "●", "result": "⎿", "prompt": "❯", "logo": "✻", "faces": [], "waiting": [],
                  "verbs": ["Thinking", "Pondering", "Working", "Reasoning", "Composing", "Considering",
                            "Brewing", "Mulling", "Crafting", "Noodling"], "name": "Finch"}


def glyph(name, key):
    """Theme glyph/branding override (bullet, result, prompt, logo, faces, verbs, name)."""
    return get(name).get("glyphs", {}).get(key) or DEFAULT_GLYPHS[key]

DEFAULT = "claude"


def get(name):
    return THEMES.get(name) or THEMES[DEFAULT]


def rich_theme(name):
    styles = dict(_BASE)
    styles.update(get(name)["rich"])
    return Theme({k: v or "none" for k, v in styles.items()})


_ANSI = ("black", "red", "green", "yellow", "blue", "magenta", "cyan", "white")


def to_pt_color(rich_style):
    """First colour of a rich style string as a prompt_toolkit fg style ('' if none)."""
    for w in (rich_style or "").split():
        if w == "on":
            break
        if w.startswith("#"):
            return f"fg:{w}"
        if w.startswith("bright_") and w[7:] in _ANSI:
            return f"fg:ansibright{w[7:]}"
        if w in _ANSI:
            return f"fg:ansi{w}"
    return ""


def swatch(name):
    r = get(name)["rich"]
    return [to_pt_color(r.get(k, "")) for k in ("accent", "ok", "err", "warn", "info")]


def pt_style_dict(name):
    p = get(name)["pt"]
    bg = f"bg:{p['menu_bg']}" if p["menu_bg"] else ""
    sel = p["menu_sel"]
    sel_style = sel if " " in sel or sel in ("reverse", "bold") else f"bg:{sel} {p.get('menu_sel_fg', '#000000')}"
    return {
        "prompt": f"{p['accent']} bold",
        "continuation": p["dim"],
        "bottom-toolbar": f"noreverse bg:default {p['dim']}",
        "tbbold": f"bold {p['fg']}".strip(),
        "tbdim": p["dim"],
        "tbwarn": p["warn"],
        "tberr": p["err"],
        "tbaccent": p["accent"],
        "completion-menu": f"{bg} {p['fg']}".strip(),
        "completion-menu.completion.current": sel_style,
        "completion-menu.meta.completion": f"{bg} {p['dim']}".strip(),
        "completion-menu.meta.completion.current": sel_style,
        # settings menu
        "sm.title": f"{p['accent']} bold",
        "sm.group": f"{p['accent']} bold",
        "sm.cursor": f"{p['accent']} bold",
        "sm.label": p["fg"],
        "sm.on": "ansigreen bold" if name != "mono" else "bold",
        "sm.off": "ansired" if name != "mono" else "",
        "sm.value": f"{p['accent']}",
        "sm.selected": "reverse" if name == "mono" else f"bold {p['fg']}".strip(),
        "sm.desc": p["dim"],
        "sm.help": p["dim"],
        "sm.tab": p["dim"],
        "sm.tab.current": f"{p['accent']} bold underline",
    }
