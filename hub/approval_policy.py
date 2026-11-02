"""Pure approval policy: does an agent tool call run, or ask the phone first. ADR-027."""

from __future__ import annotations

import ntpath
import os
import posixpath
import re
from collections.abc import Iterable, Sequence
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
GATE = "Changes the approval gate's own files"
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


# The top-level grammar is PowerShell (hub/acp_terminal.py runs every command with -Command).
_SEPARATORS = ("&&", "||", "\r\n", "\n", ";", "|", "&")


@dataclass(frozen=True)
class Segment:
    text: str
    separator: str
    groups: tuple[str, ...]
    uses_escape_or_call: bool

    @property
    def piped(self) -> bool:
        return self.separator == "|"


def parse_command(command: str) -> list[Segment]:
    """Split on chains, pipes and newlines outside quotes and groups; keep $( ), @( ), ( ) and { } bodies."""
    segments: list[Segment] = []
    start, separator, groups, irregular = 0, "", [], False
    quote, resume_quote, openers = "", [], []
    i, n = 0, len(command)

    def flush(end: int) -> None:
        text = command[start:end].strip()
        if text:
            segments.append(Segment(text, separator, tuple(groups), irregular))

    while i < n:
        ch = command[i]
        if ch == "`" and quote != "'":
            irregular = True
            i += 2
            continue
        if quote:
            if ch == quote:
                quote = ""
            elif quote == '"' and command.startswith("$(", i):
                resume_quote.append(quote)
                openers.append(i + 2)
                quote = ""
                i += 2
                continue
            i += 1
            continue
        if ch in "\"'":
            quote = ch
        elif ch in "({":
            resume_quote.append("")
            openers.append(i + 1)
        elif ch in ")}" and openers:
            body_start = openers.pop()
            quote = resume_quote.pop()
            if not openers:
                groups.append(command[body_start:i])
        elif openers:
            irregular = irregular or ch == "&"
        else:
            found = next((s for s in _SEPARATORS if command.startswith(s, i)), None)
            if found:
                flush(i)
                i += len(found)
                start, separator, groups, irregular = i, found, [], False
                continue
        i += 1
    if openers:
        groups.append(command[openers[0]:])
    flush(n)
    return segments


def shell_segments(command: str) -> list[str]:
    return [seg.text for seg in parse_command(command)]


_TOKEN = re.compile(r'"[^"]*"|\'[^\']*\'|\S+')
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_WRAPPERS = {"sudo", "sfw", "env", "time", "nohup", "call"}
_EXE_SUFFIXES = (".exe", ".cmd", ".bat", ".ps1", ".com")


def _program(token: str) -> str:
    name = ntpath.basename(token.replace("/", "\\")).lower()
    for suffix in _EXE_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _spans(segment: str) -> list[tuple[str, int, int]]:
    """Lowercased tokens with their source spans; wrappers and VAR=value prefixes dropped."""
    spans = [(m.group().strip("\"'"), m.start(), m.end()) for m in _TOKEN.finditer(segment)]
    while spans and (spans[0][0].lower() in _WRAPPERS or _ASSIGNMENT.match(spans[0][0])):
        spans = spans[1:]
    if not spans:
        return []
    prog, start, end = spans[0]
    return [(_program(prog), start, end)] + [(t.lower(), s, e) for t, s, e in spans[1:]]


def _tokens(segment: str) -> list[str]:
    return [t for t, _, _ in _spans(segment)]


def _unquote(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    return text


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
_MAX_NESTING = 4
_VERSION_OR_HELP = {"--version", "-version", "-v", "-?", "-h", "-help", "--help", "/?"}
_PS_PLAIN_FLAGS = (
    "nologo", "noprofile", "noninteractive", "noexit", "sta", "mta", "login", "help",
    "noprofileloadtime", "removeworkingdirectorytrailingcharacter",
)
_PS_VALUED_FLAGS = (
    "executionpolicy", "windowstyle", "inputformat", "outputformat", "version", "psconsolefile",
    "configurationname", "workingdirectory", "settingsfile", "custompipename", "encodedarguments",
)
_CMD_PLAIN_FLAG = re.compile(r"^/(?:s|q|d|a|u|[efv]:(?:on|off)|t:[0-9a-f]{1,2})$")
_SH_LONG_PLAIN = {
    "login", "norc", "noprofile", "posix", "verbose", "noediting", "restricted", "debugger",
    "version", "help", "pretty-print",
}
_SH_LETTERS = set("abefhkmnoprtuvxBCDEHOPTil")
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
_DELETERS = {"rm", "rmdir", "rd", "del", "erase", "remove-item", "ri", "rimraf"}
_RG_PREPROCESSOR = re.compile(r"^--pre(?:-glob)?(?:=|$)")
_PACKAGE_RUNNERS = {"npx", "bunx", "uvx", "pnpx"}
_PACKAGE_VERBS = {"install", "i", "add", "ci", "update", "upgrade", "sync", "dlx", "exec", "x"}
_JS_PMS = {"npm", "pnpm", "yarn", "bun"}
_PY_PMS = {"pip", "pip3", "uv", "pipx", "poetry", "pdm", "conda", "mamba"}
_OS_PMS = {"winget", "choco", "scoop", "apt", "apt-get", "brew", "cargo", "go", "gem", "dotnet"}


def _is_prefix(name: str, full: str) -> bool:
    return bool(name) and full.startswith(name)


def _recursive_flag(arg: str) -> bool:
    if arg in ("--recursive", "/s") or (len(arg) > 1 and "-recurse".startswith(arg)):
        return True
    return arg.startswith("-") and not arg.startswith("--") and len(arg) <= 4 and "r" in arg


def _delete_risk(prog: str, args: list[str], piped: bool) -> bool:
    if prog not in _DELETERS:
        return False
    return prog == "rimraf" or piped or any(_recursive_flag(a) for a in args)


def _git_config_override(t: list[str]) -> bool:
    i = 1
    while i < len(t) and t[i].startswith("-"):
        tok = t[i]
        if tok.startswith("--config-env") or (tok.startswith("-c") and "=" in tok):
            return True
        if tok == "-c" and i + 1 < len(t):
            if "=" in t[i + 1]:
                return True
            i += 1
        elif tok in ("--git-dir", "--work-tree", "--namespace"):
            i += 1
        i += 1
    return False


def _git_risk(t: list[str]) -> str | None:
    if _git_config_override(t):
        return DYNAMIC
    sub, args = _subcommand(t), t[1:]
    if sub in ("push", "rebase", "filter-branch", "filter-repo"):
        return GIT
    if sub == "clean":
        return DELETE
    if sub == "reset" and "--hard" in args:
        return GIT
    if sub in ("branch", "tag") and any(a in ("-d", "-D", "--delete", "-m", "-M") for a in args):
        return GIT
    if sub in ("checkout", "switch") and any(a in ("-f", "--force", "--discard-changes") for a in args):
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


Spans = list[tuple[str, int, int]]


def _joined(args: Spans, text: str) -> str:
    return " ".join(_unquote(text[start:end]) for _, start, end in args)


def _powershell_risk(args: Spans, text: str, depth: int) -> str | None:
    """PowerShell accepts any unambiguous parameter prefix, and a bare positional argument is the command."""
    i = 0
    while i < len(args):
        tok, _start, _end = args[i]
        if not tok.startswith(("-", "/")):
            return _risk_text(_joined(args[i:], text), depth + 1)
        name = tok.lstrip("-/")
        if _is_prefix(name, "encodedcommand") or name == "ec":
            return DYNAMIC
        if _is_prefix(name, "commandwithargs"):
            return _risk_text(_joined(args[i + 1:], text), depth + 1) if i + 1 < len(args) else DYNAMIC
        if _is_prefix(name, "file"):
            return DYNAMIC if i + 1 < len(args) and args[i + 1][0] == "-" else None
        plain = any(_is_prefix(name, flag) for flag in _PS_PLAIN_FLAGS)
        valued = any(_is_prefix(name, flag) for flag in _PS_VALUED_FLAGS)
        if plain == valued:
            return DYNAMIC
        i += 2 if valued else 1
    return DYNAMIC


def _cmd_risk(args: Spans, text: str, depth: int) -> str | None:
    for i, (tok, _start, _end) in enumerate(args):
        if tok in ("/c", "/k"):
            return _risk_text(_joined(args[i + 1:], text), depth + 1) if i + 1 < len(args) else DYNAMIC
        if tok == "/?":
            return None
        if not _CMD_PLAIN_FLAG.match(tok):
            return DYNAMIC
    return DYNAMIC


def _posix_shell_risk(args: Spans, text: str, depth: int) -> str | None:
    i = 0
    while i < len(args):
        _tok, start, end = args[i]
        raw = text[start:end]
        if raw == "--":
            return None if i + 1 < len(args) else DYNAMIC
        if raw.startswith("--"):
            if raw[2:] in ("rcfile", "init-file"):
                i += 2
                continue
            if raw[2:] not in _SH_LONG_PLAIN:
                return DYNAMIC
        elif raw.startswith(("-", "+")) and len(raw) > 1:
            letters = raw[1:]
            if "c" in letters:
                command = next((a for a in args[i + 1:] if not text[a[1]:a[2]].startswith(("-", "+"))), None)
                return _risk_text(_unquote(text[command[1]:command[2]]), depth + 1) if command else DYNAMIC
            if "s" in letters or not set(letters) <= _SH_LETTERS:
                return DYNAMIC
            i += 1 if letters[-1] not in "oO" else 2
            continue
        else:
            return DYNAMIC if raw == "-" else None
        i += 1
    return DYNAMIC


def _nested_shell_risk(prog: str, args: Spans, text: str, depth: int) -> str | None:
    if len(args) == 1 and args[0][0] in _VERSION_OR_HELP:
        return None
    if prog in ("powershell", "pwsh"):
        return _powershell_risk(args, text, depth)
    if prog == "cmd":
        return _cmd_risk(args, text, depth)
    return _posix_shell_risk(args, text, depth)


def _program_risk(spans: Spans, text: str, depth: int, piped: bool) -> str | None:
    t = [tok for tok, _, _ in spans]
    prog, args = t[0], t[1:]
    if prog in _SHELLS:
        return _nested_shell_risk(prog, spans[1:], text, depth)
    if prog in ("invoke-expression", "iex", "eval"):
        return DYNAMIC
    if _delete_risk(prog, args, piped):
        return DELETE
    if prog == "git":
        return _git_risk(t)
    if prog == "rg" and any(_RG_PREPROCESSOR.match(a) for a in args):
        return DYNAMIC
    if _package_risk(prog, t):
        return PACKAGE
    if prog in _NETWORK or (prog == "certutil" and "-urlcache" in args):
        return NETWORK
    if _system_risk(prog, args):
        return SYSTEM
    return None


def _risk(seg: Segment, depth: int = 0, piped: bool = False) -> str | None:
    if depth > _MAX_NESTING:
        return DYNAMIC
    spans = _spans(seg.text)
    category = _program_risk(spans, seg.text, depth, piped) if spans else None
    return category or next((r for body in seg.groups if (r := _risk_text(body, depth + 1, piped))), None)


def _risk_text(text: str, depth: int, piped: bool = False) -> str | None:
    if depth > _MAX_NESTING:
        return DYNAMIC
    return next((r for seg in parse_command(text) if (r := _risk(seg, depth, piped or seg.piped))), None)


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


def _routine(seg: Segment) -> bool:
    if seg.groups or seg.uses_escape_or_call:
        return False
    t = _tokens(seg.text)
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


def _file_rule_target(rule: str) -> tuple[str, bool] | None:
    """(target path, covers a folder) for a 'file ...' rule, or None when the rule names nothing."""
    body = rule[4:].strip()
    folder = body.endswith("*")
    target = body[:-1] if folder else body
    return (target, folder) if target.strip(" \\/") else None


def valid_rule(rule: str) -> bool:
    rule = str(rule or "").strip()
    kind = rule.split(maxsplit=1)[0] if rule else ""
    if kind == "file":
        return _file_rule_target(rule) is not None
    if kind == "mcp":
        return bool(rule[3:].strip())
    t = _tokens(rule)
    return bool(t) and t[0] != "*"


_WORDLIKE = re.compile(r"^[a-z][a-z0-9-]*$")


def similar_rule_for(segment: str) -> str | None:
    """Family rule for plain '<program> <word> ...' segments; otherwise the exact command."""
    t = _tokens(segment)
    if not t:
        return None
    parts = parse_command(segment)
    plain = len(parts) == 1 and not parts[0].groups and not parts[0].uses_escape_or_call
    if plain and t[0] not in _SHELLS and len(t) > 1:
        if _WORDLIKE.match(t[1]):
            return f"{t[0]} {t[1]} *"
        if not t[1].startswith(("-", "/")):
            return f"{t[0]} *"
    return None if t[-1] == "*" else segment.strip()


def _shell_grants(grants: Sequence[str]) -> list[str]:
    return [g for g in grants if valid_rule(g) and not g.startswith(("file ", "mcp "))]


def _pathmod(*paths: str):
    windows = any(re.match(r"^[A-Za-z]:", p) or "\\" in p for p in paths if p)
    return ntpath if windows else posixpath


def path_key(path: str) -> str:
    pm = _pathmod(path)
    return pm.normcase(pm.normpath(path))


def _under(norm_path: str, norm_dir: str, pm) -> bool:
    return norm_path == norm_dir or norm_path.startswith(norm_dir.rstrip("\\/") + pm.sep)


def _normalized(paths: Iterable[str], pm) -> list[str]:
    return [pm.normcase(pm.normpath(p)) for p in paths if p]


def _mentions_protected(segment: str, protected: Sequence[str], root: str | None) -> bool:
    """A protected path written in full, or any token that resolves to one against the session root."""
    if not protected:
        return False
    pm = _pathmod(root or "", *protected)
    wants = _normalized(protected, pm)
    flat = pm.normcase(segment)
    if any(re.search(re.escape(w) + r"(?=$|[\\/\s\"'])", flat) for w in wants):
        return True
    for m in _TOKEN.finditer(segment):
        tok = m.group().strip("\"'")
        if not tok or (not pm.isabs(tok) and not root):
            continue
        candidate = pm.normcase(pm.normpath(tok if pm.isabs(tok) else pm.join(root, tok)))
        if any(_under(candidate, w, pm) for w in wants):
            return True
    return False


def command_verdict(
    command: str, mode: str, grants: Sequence[str] = (), truncated: bool = False, *,
    protected: Sequence[str] = (), session_root: str | None = None,
) -> Verdict:
    mode = normalize_mode(mode)
    if mode == "trusted":
        return Verdict(ALLOW, "shell", command)
    if truncated:
        return Verdict(ASK, "shell", command, TRUNCATED)
    shell_rules = _shell_grants(grants)
    for seg in parse_command(command):
        if _mentions_protected(seg.text, protected, session_root):
            return Verdict(ASK, "shell", command, SYSTEM)
        if any(rule_matches(r, seg.text) for r in shell_rules):
            continue
        category = _risk(seg, 0, seg.piped) or (UNLISTED if mode == "strict" and not _routine(seg) else None)
        if category:
            return Verdict(ASK, "shell", command, category, similar_rule_for(seg.text))
    return Verdict(ALLOW, "shell", command)


_PROFILE_ROOT = re.compile(r"^(?:[a-z]:\\users\\[^\\]+|/(?:home|users)/[^/]+|/root)$")


def _root_like(folder: str, pm) -> bool:
    return pm.dirname(folder) == folder or bool(_PROFILE_ROOT.match(pm.normcase(folder)))


def _file_rule_allows(rule: str, norm_full: str, pm) -> bool:
    target = _file_rule_target(rule) if rule.startswith("file ") else None
    if target is None:
        return False
    path, folder = target
    want = pm.normcase(pm.normpath(path))
    return pm.dirname(norm_full) == want if folder else norm_full == want


def _offered_file_rule(full: str, pm) -> str:
    parent = pm.dirname(full)
    return f"file {full}" if _root_like(parent, pm) else f"file {pm.join(parent, '*')}"


def file_verdict(
    path: str, mode: str, session_root: str | None, grants: Sequence[str] = (), *,
    protected: Sequence[str] = (),
) -> Verdict:
    mode = normalize_mode(mode)
    if mode == "trusted":
        return Verdict(ALLOW, "file", path)
    pm = _pathmod(path, session_root or "")
    if not path or not (session_root or pm.isabs(path)):
        return Verdict(ASK, "file", path, OUTSIDE)
    full = pm.normpath(path if pm.isabs(path) else pm.join(session_root or "", path))
    if pm is os.path:
        full = os.path.realpath(full)
    norm_full = pm.normcase(full)
    if any(_under(norm_full, w, pm) for w in _normalized(protected, pm)):
        return Verdict(ASK, "file", path, GATE)
    if not session_root:
        return Verdict(ASK, "file", path, OUTSIDE)
    root = pm.normpath(session_root)
    if pm is os.path:
        root = os.path.realpath(root)
    if _under(norm_full, pm.normcase(root), pm):
        return Verdict(ALLOW, "file", path)
    if any(_file_rule_allows(rule, norm_full, pm) for rule in grants):
        return Verdict(ALLOW, "file", path)
    return Verdict(ASK, "file", path, OUTSIDE, _offered_file_rule(full, pm))


_SHELL_TOOLS = {"run_terminal_command", "bash", "shell", "powershell", "run_command", "execute_command"}
_FILE_WORDS = ("write", "edit", "replace", "patch", "delete", "create", "move", "rename", "remove")
_PATH_KEYS = {
    "path", "file", "filename", "filepath", "target", "targetfile", "source", "src", "dest",
    "destination", "new", "old", "notebookpath", "directory", "dir", "folder",
}
_PATH_KEY_SUFFIXES = ("path", "file", "filename", "dir", "directory", "folder")
_CONTENT_KEY_SUFFIXES = ("string", "content", "contents", "text", "data", "body", "code", "pattern", "replacement")
_ABSOLUTE_WINDOWS = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)[^\r\n]*$")
_ABSOLUTE_POSIX = re.compile(r"^/[^\r\n]*$")


def _key_name(key: str) -> str:
    return re.sub(r"[^a-z]", "", key.lower())


def _file_targets(value: Any, posix: bool, key: str = "", depth: int = 0) -> list[str]:
    """Every string under a path-like key, plus any other string that is itself an absolute path."""
    if depth > 3:
        return []
    if isinstance(value, dict):
        return [p for k, v in value.items() for p in _file_targets(v, posix, str(k), depth + 1)]
    if isinstance(value, list):
        return [p for v in value for p in _file_targets(v, posix, key, depth + 1)]
    if not isinstance(value, str) or not value:
        return []
    name = _key_name(key)
    if name in _PATH_KEYS or name.endswith(_PATH_KEY_SUFFIXES):
        return [value]
    if name.endswith(_CONTENT_KEY_SUFFIXES):
        return []
    absolute = _ABSOLUTE_WINDOWS.match(value) or (posix and _ABSOLUTE_POSIX.match(value))
    return [value] if absolute else []


def classify(
    tool_name: str,
    tool_input: dict[str, Any] | None,
    *,
    mode: str,
    session_root: str | None,
    grants: Sequence[str] = (),
    truncated: bool = False,
    protected: Sequence[str] = (),
) -> Verdict:
    name = str(tool_name or "")
    lname = name.lower()
    tin = tool_input if isinstance(tool_input, dict) else {}
    if lname in _SHELL_TOOLS:
        return command_verdict(
            str(tin.get("command") or ""), mode, grants, truncated, protected=protected, session_root=session_root
        )
    if lname.startswith("mcp_"):
        rule = f"mcp {name}"
        if normalize_mode(mode) == "strict" and rule not in grants:
            return Verdict(ASK, "tool", name, MCP, rule)
        return Verdict(ALLOW, "tool", name)
    if any(w in lname for w in _FILE_WORDS):
        posix = bool(session_root) and _pathmod(session_root) is posixpath
        targets = _file_targets(tin, posix)
        if targets:
            verdicts = (file_verdict(p, mode, session_root, grants, protected=protected) for p in targets)
            return next((v for v in verdicts if v.action != ALLOW), Verdict(ALLOW, "file", targets[0]))
    return Verdict(ALLOW, "tool", name)
