"""Pure approval policy: does an agent tool call run, or ask the phone first. ADR-027."""

from __future__ import annotations

import ntpath
import os
import posixpath
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

MODES = ("balanced", "strict", "trusted")
DEFAULT_MODE = "balanced"
ALLOW, ASK = "allow", "ask"

DELETE = "Deletes files recursively"
GIT = "Pushes, rewrites or discards git changes"
PACKAGE = "Installs or runs downloaded packages"
NETWORK = "Transfers data over the network"
SYSTEM = "Changes system settings or other programs"
DYNAMIC = "Runs dynamically generated code"
TRUNCATED = "Command too long to inspect"
UNLISTED = "Not on the routine list (Strict mode)"
OUTSIDE = "Changes a file outside the project"
MCP = "MCP tool (Strict mode)"


@dataclass(frozen=True)
class Verdict:
    action: str
    kind: str
    summary: str
    category: str = ""
    similar_rule: str | None = None


def normalize_mode(mode: str | None) -> str:
    m = str(mode or "").strip().lower()
    return m if m in MODES else DEFAULT_MODE


_SEPARATORS = ("&&", "||", "\r\n", "\n", ";", "|", "&")


def shell_segments(command: str) -> list[str]:
    """Split on chains, pipes and newlines outside quotes."""
    segments, current, quote, i = [], [], "", 0
    while i < len(command):
        ch = command[i]
        if quote:
            current.append(ch)
            quote = "" if ch == quote else quote
            i += 1
            continue
        if ch in "\"'":
            quote = ch
            current.append(ch)
            i += 1
            continue
        sep = next((s for s in _SEPARATORS if command.startswith(s, i)), None)
        if sep:
            segments.append("".join(current).strip())
            current, i = [], i + len(sep)
            continue
        current.append(ch)
        i += 1
    segments.append("".join(current).strip())
    return [s for s in segments if s]


_TOKEN = re.compile(r'"[^"]*"|\'[^\']*\'|\S+')
_WRAPPERS = {"sudo", "sfw", "env", "time", "nohup", "call"}
_EXE_SUFFIXES = (".exe", ".cmd", ".bat", ".ps1", ".com")


def _program(token: str) -> str:
    name = ntpath.basename(token.replace("/", "\\")).lower()
    for suffix in _EXE_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _tokens(segment: str) -> list[str]:
    raw = [t.strip("\"'") for t in _TOKEN.findall(segment)]
    while raw and (raw[0].lower() in _WRAPPERS or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", raw[0])):
        raw = raw[1:]
    if not raw:
        return []
    return [_program(raw[0])] + [t.lower() for t in raw[1:]]


def _subcommand(t: list[str]) -> str:
    rest = iter(t[1:])
    for tok in rest:
        if tok in ("-c", "-C"):
            next(rest, None)
            continue
        if not tok.startswith("-"):
            return tok
    return ""


_SHELLS = {"powershell", "pwsh", "cmd", "bash", "sh", "zsh"}
_SHELL_FLAGS = {"-c", "-command", "/c", "/k", "-commandwithargs"}
_NETWORK = {
    "curl", "wget", "invoke-webrequest", "iwr", "invoke-restmethod", "irm", "scp", "sftp",
    "ftp", "tftp", "ssh", "rsync", "start-bitstransfer", "nc", "ncat", "bitsadmin",
}
_SYSTEM = {
    "schtasks", "new-service", "set-service", "stop-service", "start-service", "remove-service",
    "restart-service", "stop-process", "spps", "taskkill", "kill", "pkill", "killall", "shutdown",
    "restart-computer", "stop-computer", "set-executionpolicy", "netsh", "bcdedit", "diskpart",
    "format", "format-volume", "takeown", "icacls", "setx", "new-netfirewallrule",
    "register-scheduledtask", "unregister-scheduledtask", "wmic", "vssadmin", "wevtutil",
}
_PACKAGE_RUNNERS = {"npx", "bunx", "uvx", "pnpx"}
_PACKAGE_VERBS = {"install", "i", "add", "ci", "update", "upgrade", "sync", "dlx", "exec", "x"}
_JS_PMS = {"npm", "pnpm", "yarn", "bun"}
_PY_PMS = {"pip", "pip3", "uv", "pipx", "poetry", "pdm", "conda", "mamba"}
_OS_PMS = {"winget", "choco", "scoop", "apt", "apt-get", "brew", "cargo", "go", "gem", "dotnet"}


def _delete_risk(prog: str, args: list[str]) -> bool:
    if prog == "rm":
        return any(
            a in ("--recursive", "-recurse")
            or (a.startswith("-") and not a.startswith("--") and len(a) <= 4 and "r" in a)
            for a in args
        )
    if prog in ("rmdir", "rd", "del", "erase"):
        return "/s" in args
    if prog in ("remove-item", "ri"):
        return any(a.startswith("-r") for a in args)
    return prog == "rimraf"


def _git_risk(t: list[str]) -> str | None:
    sub, args = _subcommand(t), t[1:]
    if sub in ("push", "rebase", "filter-branch", "filter-repo"):
        return GIT
    if sub == "clean":
        return DELETE
    if sub == "reset" and "--hard" in args:
        return GIT
    if sub in ("branch", "tag") and any(a in ("-d", "-D", "--delete", "-m", "-M") for a in args):
        return GIT
    if sub == "checkout" and ("--" in args or "." in args):
        return GIT
    if sub == "restore" and not ("--staged" in args and "--worktree" not in args):
        return GIT
    if sub == "stash" and any(a in ("drop", "clear") for a in args):
        return GIT
    if sub == "commit" and "--amend" in args:
        return GIT
    return None


def _package_risk(prog: str, t: list[str]) -> bool:
    sub = t[1] if len(t) > 1 else ""
    if prog in _PACKAGE_RUNNERS:
        return True
    if prog in _JS_PMS:
        return (prog == "yarn" and not sub) or sub in _PACKAGE_VERBS
    if prog == "pipx" and sub == "run":
        return True
    if prog in _PY_PMS:
        return any(a in ("install", "add", "sync", "upgrade") for a in t[1:3])
    if prog in ("python", "python3", "py") and "-m" in t:
        mod = t[t.index("-m") + 1] if t.index("-m") + 1 < len(t) else ""
        return mod == "ensurepip" or (mod == "pip" and "install" in t)
    if prog in _OS_PMS:
        return any(a in ("install", "upgrade") for a in t[1:3]) or (prog == "dotnet" and sub == "add")
    return prog in ("install-module", "install-package", "install-script")


def _system_risk(prog: str, args: list[str]) -> bool:
    if prog in _SYSTEM:
        return True
    if prog == "reg":
        return bool(args) and args[0] in ("add", "delete", "import", "copy", "restore", "load")
    if prog == "sc":
        return bool(args) and args[0] in ("create", "delete", "config", "stop", "start", "failure")
    if prog in ("set-itemproperty", "new-itemproperty", "remove-itemproperty", "sp"):
        return any(a.startswith(("hklm:", "hkcu:", "registry::")) for a in args)
    if prog in ("start-process", "saps", "start"):
        return "-verb" in args and "runas" in args
    return any(a.startswith("[environment]::setenvironmentvariable") for a in [prog, *args])


_INNER_COMMAND = re.compile(r"(?is)(?:^|\s)(?:-c|-command|/c|/k|-commandwithargs)\s+(.*)$")


def _nested_shell_risk(prog: str, args: list[str], segment: str, depth: int) -> str | None:
    if prog in ("powershell", "pwsh") and any(a in ("-encodedcommand", "-enc", "-e", "-ec") for a in args):
        return DYNAMIC
    if not any(a in _SHELL_FLAGS for a in args):
        reads_stdin = not [a for a in args if not a.startswith("-")]
        return DYNAMIC if reads_stdin else None
    match = _INNER_COMMAND.search(segment)
    if not match or depth > 2:
        return None
    inner = match.group(1).strip().strip("\"'")
    return next((r for s in shell_segments(inner) if (r := _risk(s, depth + 1))), None)


def _risk(segment: str, depth: int = 0) -> str | None:
    t = _tokens(segment)
    if not t:
        return None
    prog, args = t[0], t[1:]
    if prog in _SHELLS:
        return _nested_shell_risk(prog, args, segment, depth)
    if prog in ("invoke-expression", "iex", "eval"):
        return DYNAMIC
    if _delete_risk(prog, args):
        return DELETE
    if prog == "git":
        return _git_risk(t)
    if _package_risk(prog, t):
        return PACKAGE
    if prog in _NETWORK or (prog == "certutil" and "-urlcache" in args):
        return NETWORK
    if _system_risk(prog, args):
        return SYSTEM
    return None


_ROUTINE = {
    "ls", "dir", "get-childitem", "gci", "cat", "type", "get-content", "gc", "head", "tail",
    "more", "wc", "tree", "pwd", "get-location", "cd", "set-location", "echo", "write-output",
    "write-host", "findstr", "rg", "grep", "select-string", "sls", "where", "which",
    "get-command", "gcm", "test-path", "resolve-path", "get-item", "stat", "du", "df", "sort",
    "uniq", "cut", "diff", "measure-object", "select-object", "format-table", "format-list",
    "out-string", "pytest", "tsc", "eslint", "mypy", "pyright", "ruff",
}
_GIT_ROUTINE = {
    "status", "diff", "log", "show", "rev-parse", "ls-files", "blame", "describe", "grep",
    "shortlog", "fetch", "reflog", "branch", "remote",
}
_BUILD_SUBS = {
    "cargo": {"build", "test", "check", "clippy", "fmt", "tree"},
    "go": {"build", "test", "vet", "fmt", "list", "env", "version"},
    "dotnet": {"build", "test"},
}
_JS_SCRIPTS = {"test", "lint", "build", "typecheck", "check", "format"}
_PY_MODULES = {"pytest", "unittest", "mypy", "ruff", "pyright", "black", "compileall"}


def _routine(segment: str) -> bool:
    t = _tokens(segment)
    if not t:
        return True
    prog, args = t[0], t[1:]
    if args in (["--version"], ["-v"], ["-V"], ["--help"]):
        return True
    if prog in _ROUTINE:
        return True
    if prog == "git":
        return _subcommand(t) in _GIT_ROUTINE and _git_risk(t) is None
    if prog in ("python", "python3", "py"):
        return "-m" in args and args.index("-m") + 1 < len(args) and args[args.index("-m") + 1] in _PY_MODULES
    if prog in _JS_PMS:
        sub = args[0] if args else ""
        return sub in _JS_SCRIPTS or (sub == "run" and len(args) > 1 and args[1] in _JS_SCRIPTS)
    return prog in _BUILD_SUBS and bool(args) and args[0] in _BUILD_SUBS[prog]


def rule_matches(rule: str, segment: str) -> bool:
    want, have = _tokens(rule), _tokens(segment)
    if want and want[-1] == "*":
        return have[: len(want) - 1] == want[:-1]
    return bool(want) and have == want


_WORDLIKE = re.compile(r"^-{0,2}[a-z][a-z0-9-]*$")


def similar_rule_for(segment: str) -> str:
    t = _tokens(segment)
    if len(t) > 1 and _WORDLIKE.match(t[1]):
        return f"{t[0]} {t[1]} *"
    return f"{t[0]} *" if t else "*"


def _shell_grants(grants: Sequence[str]) -> list[str]:
    return [g for g in grants if not g.startswith(("file ", "mcp "))]


def command_verdict(
    command: str, mode: str, grants: Sequence[str] = (), truncated: bool = False
) -> Verdict:
    mode = normalize_mode(mode)
    if mode == "trusted":
        return Verdict(ALLOW, "shell", command)
    if truncated:
        return Verdict(ASK, "shell", command, TRUNCATED)
    shell_rules = _shell_grants(grants)
    for seg in shell_segments(command):
        if any(rule_matches(r, seg) for r in shell_rules):
            continue
        category = _risk(seg) or (UNLISTED if mode == "strict" and not _routine(seg) else None)
        if category:
            return Verdict(ASK, "shell", command, category, similar_rule_for(seg))
    return Verdict(ALLOW, "shell", command)


def _pathmod(*paths: str):
    windows = any(re.match(r"^[A-Za-z]:", p) or "\\" in p for p in paths if p)
    return ntpath if windows else posixpath


def path_key(path: str) -> str:
    pm = _pathmod(path)
    return pm.normcase(pm.normpath(path))


def file_verdict(
    path: str, mode: str, session_root: str | None, grants: Sequence[str] = ()
) -> Verdict:
    mode = normalize_mode(mode)
    if mode == "trusted":
        return Verdict(ALLOW, "file", path)
    if not path or not session_root:
        return Verdict(ASK, "file", path, OUTSIDE)
    pm = _pathmod(path, session_root)
    full = pm.normpath(path if pm.isabs(path) else pm.join(session_root, path))
    root = pm.normpath(session_root)
    if pm is os.path:
        full, root = os.path.realpath(full), os.path.realpath(root)
    norm_full, norm_root = pm.normcase(full), pm.normcase(root)
    try:
        if pm.commonpath([norm_full, norm_root]) == norm_root:
            return Verdict(ALLOW, "file", path)
    except ValueError:
        pass
    for rule in grants:
        if rule.startswith("file ") and rule.endswith("*"):
            prefix = pm.normcase(rule[5:-1])
            if norm_full.startswith(prefix):
                return Verdict(ALLOW, "file", path)
    return Verdict(ASK, "file", path, OUTSIDE, f"file {pm.join(pm.dirname(full), '*')}")


_SHELL_TOOLS = {"run_terminal_command", "bash", "shell", "powershell", "run_command", "execute_command"}
_PATH_KEYS = ("file_path", "path", "target_file", "filePath", "target", "notebook_path", "filename")
_FILE_WORDS = ("write", "edit", "replace", "patch", "delete", "create", "move", "rename", "remove")


def classify(
    tool_name: str,
    tool_input: dict[str, Any] | None,
    *,
    mode: str,
    session_root: str | None,
    grants: Sequence[str] = (),
    truncated: bool = False,
) -> Verdict:
    name = str(tool_name or "")
    lname = name.lower()
    tin = tool_input if isinstance(tool_input, dict) else {}
    if lname in _SHELL_TOOLS:
        return command_verdict(str(tin.get("command") or ""), mode, grants, truncated)
    if lname.startswith("mcp_"):
        rule = f"mcp {name}"
        if normalize_mode(mode) == "strict" and rule not in grants:
            return Verdict(ASK, "tool", name, MCP, rule)
        return Verdict(ALLOW, "tool", name)
    path = next((str(tin[k]) for k in _PATH_KEYS if tin.get(k)), "")
    if path and any(w in lname for w in _FILE_WORDS):
        return file_verdict(path, mode, session_root, grants)
    return Verdict(ALLOW, "tool", name)
