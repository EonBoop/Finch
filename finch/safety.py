"""The safety net. Each ring has exactly one job.

ring 1  irreversible ops        hard refuse, no way out (not even "allow all")
ring 2  behaviour tiering       uninstall / env mutation: ask when attended, deny when
                                unattended; recursive delete only inside the workdir
ring 3  boundary confirmation   out-of-workdir paths and sensitive files -> ask the human
ring 4  rollback everywhere     backups before writes, trash instead of delete, clobber
                                guard (implemented in tools.py)

`unsafe` bypasses ring 2 only. Shell parsing is heuristic: `python -c` can reach outside
the workdir; rings 1/2 and the rollback ring are the backstop.
"""
import os
import re

DANGER_CMD = (
    "sudo rm",
    "of=/dev/sd", "of=/dev/nvme", "> /dev/sd", "shred ", "srm ",
    "init 0", ":(){ :|:& };:", "chmod -r 777 /", "curl | bash", "curl | sh",
    "wget | bash", "wget | sh", "| sudo bash", "| sudo sh",
    "format c:", "diskpart", "vssadmin delete", "bcdedit", "cipher /w", "del /f /s /q c:",
    "wsl --unregister", "dism ", "pnputil /delete-driver", "reg delete", "reg add",
    "git push --force", "git push -f",
)
# rm -rf of the filesystem root or home itself (not of paths below them: those go to ring 2/3)
DANGER_RM = re.compile(r"\brm\s+(-\S+\s+)*(/|/\*|~|~/|~/\*|\$home|\$home/|\$home/\*)(\s|$|;|&|\|)")
DANGER_WORDS = re.compile(r"\b(mkfs(\.\w+)?|wipefs|shutdown|reboot|halt|poweroff|userdel|deluser|"
                          r"diskpart|bcdedit)\b")
_PIPE_EXEC_RE = re.compile(r"\b(curl|wget)\b[^|;&]*\|\s*(sudo\s+)?(ba|z|)sh\b", re.I)

UNINSTALL_RE = re.compile(
    r"\b(pip3?|python3?\s+-m\s+pip|uv\s+pip)\s+uninstall\b|\bnpm\s+(uninstall|rm|remove)\b|"
    r"\byarn\s+remove\b|\bpnpm\s+remove\b|\bapt(-get)?\s+(remove|purge|autoremove)\b|"
    r"\byum\s+remove\b|\bdnf\s+remove\b|\bsnap\s+remove\b|\bpacman\s+-R|\bzypper\s+(remove|rm)\b|"
    r"\bbrew\s+(uninstall|remove)\b|\bflatpak\s+uninstall\b|\bdpkg\s+(-r|-P|--remove|--purge)\b|"
    r"\brpm\s+-e\b|\bmake\s+uninstall\b|\bconda\s+(remove|uninstall)\b|\bcargo\s+uninstall\b|"
    r"\bwinget\s+(uninstall|remove)\b|\bchoco\s+uninstall\b|\bmsiexec\s+/x\b", re.I)
ENV_MUTATE_RE = re.compile(
    r"\bsystemctl\s+(stop|disable|mask|enable)\b|\bcrontab\s+-r\b|\bcrontab\s+-e\b|"
    r"\blaunchctl\s+(remove|disable|unload)\b|\bsetx\b|\bschtasks\b|\bsc\s+delete\b|"
    r">>?\s*~/\.(bashrc|zshrc|profile|bash_profile)\b|\bupdate-alternatives\b|\bchsh\b", re.I)
RECURSE_DELETE_RE = re.compile(
    r"\brm\s+(-[a-z]*r[a-z]*|--recursive)\b|\brm\s+-\w+\s+-\w*r\b|\brmdir\s+/s\b|"
    r"\brd\s+/s\b|\bremove-item\b[^&|;]*-recurse", re.I)
FIND_DELETE_RE = re.compile(r"\bfind\s+(\S+)[^&|;]*-delete\b", re.I)

SENSITIVE = ("/.ssh/", "/.aws/", "/.gnupg/", "/.kube/", "/.docker/config", ".env", ".pem",
             "id_rsa", "id_ed25519", ".netrc", ".pgpass", "credentials", "/secrets",
             "/.finch/config.json", "/.finch/mcp.json", "/.finch/memory.json",
             "/etc/shadow", "/etc/sudoers")
SYSTEM_DIRS = ("/usr/", "/etc/", "/bin/", "/sbin/", "/boot/", "/lib/", "/lib64/", "/root/",
               "/sys/", "/proc/", "/dev/", "/var/lib/", "/opt/", "\\windows\\",
               "\\program files\\", "\\system32\\", "/mnt/c/windows/")
BASH_ESCAPE = (
    r"(^|[\s;&|])cd\s+(/|~|\.\.)", r"\.ssh\b", r"\.aws\b", r"(^|\s)\.env\b",
    r"\b(mv|cp|rsync)\s+[^&|;]*\s+(~|/home/|/etc/|/usr/|/opt/|/root)",
    r">\s*(~|/home/|/etc/|/usr/)", r"\btee\s+(-a\s+)?(~|/home/|/etc/|/usr/)",
    r"\bsudo\b", r"(^|\s)/mnt/[a-z]/",
)
FILE_TOOLS = ("create_file", "read_file", "edit_file", "append_file", "delete_file",
              "list_dir", "search_files")
WRITE_TOOLS = ("create_file", "edit_file", "append_file", "delete_file")


def safe_path(workdir, path, allow_dirs=()):
    """realpath folds `..` and symlinks before the boundary check, so a link inside the
    workdir cannot become an escape hatch. Returns (real_abs, inside)."""
    p = os.path.expanduser(str(path or ""))
    real = os.path.realpath(os.path.join(workdir, p))
    for d in [workdir, *allow_dirs]:
        base = os.path.realpath(d)
        try:
            if os.path.commonpath([base, real]) == base:
                return real, True
        except ValueError:
            continue
    return real, False


def is_sensitive(real):
    p = str(real).lower().replace("\\", "/")
    name = os.path.basename(p)
    if name in (".env.example", ".env.sample", ".env.template"):
        return False
    return any(s in p for s in SENSITIVE)


def delete_targets(cmd):
    m = RECURSE_DELETE_RE.search(cmd)
    if not m:
        return []
    tail = re.split(r"[;&|]", cmd[m.start():], maxsplit=1)[0]
    out = []
    for t in tail.split():
        t = t.strip().strip('"').strip("'")
        if not t or t.lower() in ("rm", "rmdir", "rd", "remove-item") or t.startswith("-"):
            continue
        if re.fullmatch(r"/[a-z]", t.lower()):
            continue
        out.append(t)
    return out


class Gate:
    """Holds per-turn "allow all" grants and the human-confirmation callback."""

    def __init__(self, settings, confirm=None):
        self.s = settings
        self.confirm = confirm          # fn(tool, target, action) -> "yes" | "all" | "no"
        self.allow_all = set()
        self.allow_dirs = set()

    def new_turn(self):
        self.allow_all.clear()

    def ask(self, tool, target, action):
        if self.s.unattended or self.confirm is None:
            return False, (f"[safety gate: {action} '{target}' needs human confirmation and "
                           f"no human is attached; refused.]")
        ans = self.confirm(tool, target, action)
        if ans == "all":
            self.allow_all.add(tool)
            return True, ""
        if ans == "yes":
            return True, ""
        return False, f"[safety gate: the user declined: {action} '{target}'.]"

    def risk_classify(self, cmd, workdir):
        """Ring 2."""
        if UNINSTALL_RE.search(cmd):
            if self.s.allow_env_mutation:
                return None
            ok, msg = self.ask("run_bash", cmd[:100], "uninstall software")
            return None if ok else (msg + " Uninstalling dependencies is almost never needed; "
                                    "work around it inside the project.")
        if ENV_MUTATE_RE.search(cmd):
            if self.s.allow_env_mutation:
                return None
            ok, msg = self.ask("run_bash", cmd[:100], "modify the system environment")
            return None if ok else msg
        if RECURSE_DELETE_RE.search(cmd):
            targets = delete_targets(cmd)
            if not targets:
                return "[safety net: recursive delete with no explicit target was refused. Name the exact path.]"
            for t in targets:
                if any(ch in t for ch in "*?") and "/" not in t:
                    continue        # glob relative to cwd stays inside the workdir
                _, inside = safe_path(workdir, t, self.allow_dirs)
                if not inside:
                    return (f"[safety net: recursive delete target '{t[:80]}' is outside the working "
                            f"directory; refused. Recursive deletes inside the workdir are fine.]")
        mf = FIND_DELETE_RE.search(cmd)
        if mf:
            _, inside = safe_path(workdir, mf.group(1), self.allow_dirs)
            if not inside:
                return f"[safety net: find -delete on '{mf.group(1)[:80]}' is outside the working directory; refused.]"
        return None

    def check(self, name, args, workdir):
        """Returns a refusal message, or None to allow."""
        try:
            if name in ("run_bash", "run_python"):
                cmd = str(args.get("command") or args.get("code") or "")
                low = " ".join(cmd.lower().split())
                if (any(d in low for d in DANGER_CMD) or DANGER_WORDS.search(low) or DANGER_RM.search(low)
                        or _PIPE_EXEC_RE.search(low)):
                    return f"[safety gate: irreversible/dangerous operation refused: {cmd[:100]}]"
                if not self.s.unsafe:
                    r = self.risk_classify(cmd, workdir)
                    if r:
                        return r
                if name == "run_bash" and name not in self.allow_all and self.s.on("boundary_confirm"):
                    for pat in BASH_ESCAPE:
                        if re.search(pat, low):
                            ok, msg = self.ask(name, cmd[:120], "run a command that reaches outside the workdir")
                            return None if ok else msg
                return None
            if name in FILE_TOOLS:
                path = str(args.get("path") or "")
                if not path:
                    return None
                real, inside = safe_path(workdir, path, self.allow_dirs)
                low = real.lower() + ("/" if os.path.isdir(real) else "")
                if name in WRITE_TOOLS and any(d in low for d in SYSTEM_DIRS):
                    return f"[safety gate: writing into a system directory is refused: {real}]"
                if is_sensitive(real):
                    if name in WRITE_TOOLS:
                        return f"[safety gate: {real} matches a sensitive pattern; writes are always refused.]"
                    if name not in self.allow_all and self.s.on("boundary_confirm"):
                        ok, msg = self.ask(name, real, "read a sensitive file")
                        return None if ok else msg
                    return None
                if not inside and name not in self.allow_all and self.s.on("boundary_confirm"):
                    ok, msg = self.ask(name, real, "access a path outside the workdir")
                    if ok:
                        return None
                    return (msg + f" Working directory: {os.path.abspath(workdir)}. "
                            "Inputs are inside it: use list_dir(path='.') and relative paths.")
            return None
        except Exception as e:
            return f"[safety gate error: {e}; refused]"
