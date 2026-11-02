"""Approval policy: does an agent tool call run, or ask the phone first. ADR-027."""

from __future__ import annotations

import ntpath
import os
import posixpath
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import unquote

MODES = ("balanced", "strict", "trusted")
DEFAULT_MODE = "balanced"
ALLOW, ASK = "allow", "ask"
MAX_COMMAND_CHARS = 4096

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
GITDIR = "Changes the repository's .git folder (Strict mode)"
NO_TARGET = "File tool without a recognisable path"
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


# Lexer. The top-level grammar is PowerShell: hub/acp_terminal.py runs every command with -Command.

_SINGLE_QUOTES = "'‘’‚‛"
_DOUBLE_QUOTES = '"“”„'
_QUOTES = _SINGLE_QUOTES + _DOUBLE_QUOTES
_DASHES = "".join(chr(c) for c in (0x2013, 0x2014, 0x2015))
_STREAMS = "123456*"
_MAX_NESTING = 8
_ASSIGN_OPS = {"=", "+=", "-=", "*=", "/=", "%=", "??="}
_VARIABLE = r"(?:\[[^\]]*\])?\$[\w:{}.]+"
_VARIABLE_ONLY = re.compile(rf"^{_VARIABLE}$")
_GLUED_ASSIGNMENT = re.compile(rf"^{_VARIABLE}(?:[-+*/%]|\?\?)?=(.*)$", re.S)
_ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_WRAPPERS = {"sudo", "sfw", "env", "time", "nohup", "call"}


class TooComplex(Exception):
    """More nesting than the lexer models; callers fail closed."""


@dataclass(frozen=True)
class Group:
    body: str
    statements: tuple[Statement, ...]


@dataclass(frozen=True)
class Token:
    text: str
    raw: str
    kind: str = "word"
    quoted: bool = False
    groups: tuple[Group, ...] = ()


@dataclass(frozen=True)
class Statement:
    raw: str
    separator: str
    tokens: tuple[Token, ...]
    irregular: bool
    malformed: bool

    @property
    def words(self) -> tuple[Token, ...]:
        return tuple(t for t in self.tokens if t.kind == "word")

    @property
    def groups(self) -> tuple[Group, ...]:
        return tuple(g for t in self.tokens for g in t.groups)

    @property
    def redirects(self) -> tuple[Token, ...]:
        return tuple(t for t in self.tokens if t.kind == "redirect")

    @property
    def stop_parsing(self) -> bool:
        return any(t.kind == "stop" for t in self.tokens)

    @property
    def piped(self) -> bool:
        return self.separator == "|"

    @property
    def plain(self) -> bool:
        return not (self.groups or self.irregular or self.malformed or self.redirects or self.stop_parsing)


def _newline_at(s: str, i: int) -> str:
    if s.startswith("\r\n", i):
        return "\r\n"
    return s[i] if i < len(s) and s[i] in "\r\n" else ""


def _line_end(s: str, i: int) -> int:
    ends = [k for k in (s.find("\r", i), s.find("\n", i)) if k >= 0]
    return min(ends) if ends else len(s)


def _here_string_opens(s: str, j: int) -> bool:
    while j < len(s) and s[j] in " \t":
        j += 1
    return bool(_newline_at(s, j))


class _Lexer:
    def __init__(self, text: str, depth: int = 0) -> None:
        if depth > _MAX_NESTING:
            raise TooComplex
        self.s, self.n, self.depth = text, len(text), depth

    def statements(self) -> list[Statement]:
        return self._run(0, "")[0]

    def _run(self, i: int, closer: str) -> tuple[list[Statement], int, bool]:
        s, n = self.s, self.n
        out: list[Statement] = []
        tokens: list[Token] = []
        frags: list[list] = []
        state = {"word_start": i, "stmt_start": i, "separator": "", "irregular": False, "malformed": False, "redirect": ""}

        def add(text: str, quoted: bool = False, groups: Sequence[Group] = (), at: int = 0) -> None:
            if not frags:
                state["word_start"] = at
            if frags and not quoted and not groups and not frags[-1][1] and not frags[-1][2]:
                frags[-1][0] += text
            else:
                frags.append([text, quoted, list(groups)])

        def end_word(end: int) -> None:
            if not frags:
                return
            text = "".join(f[0] for f in frags)
            quoted = any(f[1] for f in frags)
            groups = tuple(g for f in frags for g in f[2])
            raw = s[state["word_start"]:end]
            first_unquoted = not frags[0][1]
            frags.clear()
            if state["redirect"]:
                tokens.append(Token(text, raw, "redirect", quoted, groups))
                state["redirect"] = ""
            elif first_unquoted and text.startswith("-") and ":" in text:
                name, _, value = text.partition(":")
                tokens.append(Token(name, raw))
                if value:
                    tokens.append(Token(value, raw, "word", quoted, groups))
            else:
                tokens.append(Token(text, raw, "word", quoted, groups))

        def end_statement(end: int, next_separator: str) -> None:
            nonlocal tokens
            end_word(end)
            if state["redirect"]:
                state["malformed"] = True
            raw = s[state["stmt_start"]:end].strip()
            if tokens or raw:
                out.append(_statement(raw, state["separator"], tokens, state["irregular"], state["malformed"]))
            tokens = []
            state.update(separator=next_separator, irregular=False, malformed=False, redirect="")

        while i < n:
            ch = s[i]
            if closer and ch == closer:
                end_statement(i, "")
                return out, i + 1, True
            if ch == "`":
                continuation = _newline_at(s, i + 1)
                if continuation:
                    end_word(i)
                    i += 1 + len(continuation)
                    continue
                state["irregular"] = True
                if i + 1 < n:
                    add(s[i + 1], at=i)
                i += 2
                continue
            if ch in " \t":
                end_word(i)
                i += 1
                continue
            newline = _newline_at(s, i)
            if newline:
                end_statement(i, "\n")
                i += len(newline)
                state["stmt_start"] = i
                continue
            if ch == "#" and not frags:
                i = _line_end(s, i)
                continue
            if s.startswith("<#", i):
                end_word(i)
                j = s.find("#>", i + 2)
                if j < 0:
                    state["malformed"] = True
                    i = n
                    continue
                i = j + 2
                continue
            if s.startswith("--%", i) and not frags and (i + 3 == n or s[i + 3] in " \t\r\n"):
                j = _line_end(s, i)
                tokens.append(Token(s[i + 3:j].strip(), s[i:j], "stop"))
                state["irregular"] = True
                i = j
                continue
            if ch == "@" and i + 1 < n and s[i + 1] in _QUOTES and _here_string_opens(s, i + 2):
                text, groups, j, closed = self._here_string(i)
                add(text, True, groups, at=i)
                state["irregular"] = True
                state["malformed"] |= not closed
                i = j
                continue
            if ch in _SINGLE_QUOTES:
                text, j, closed = self._single(i)
                add(text, True, at=i)
                state["malformed"] |= not closed
                i = j
                continue
            if ch in _DOUBLE_QUOTES:
                text, groups, j, closed = self._double(i)
                add(text, True, groups, at=i)
                state["malformed"] |= not closed
                i = j
                continue
            if ch in "({":
                group, j, closed = self._group(i)
                add(s[i:j], False, (group,), at=i)
                state["malformed"] |= not closed
                i = j
                continue
            if ch == ">" and (not frags or (len(frags) == 1 and not frags[0][1] and frags[0][0] in _STREAMS)):
                frags.clear()
                j = i + 1 + (1 if s.startswith(">", i + 1) else 0)
                if s.startswith("&", j) and j + 1 < n and s[j + 1] in _STREAMS:
                    tokens.append(Token(s[i:j + 2], s[i:j + 2], "merge"))
                    i = j + 2
                    continue
                state["redirect"] = s[i:j]
                i = j
                continue
            if s.startswith(("&&", "||"), i):
                end_statement(i, s[i:i + 2])
                i += 2
                state["stmt_start"] = i
                continue
            if ch in "|;&":
                end_statement(i, ch)
                i += 1
                state["stmt_start"] = i
                continue
            add("-" if ch in _DASHES else ch, at=i)
            i += 1
        if closer:
            state["malformed"] = True
        end_statement(n, "")
        return out, n, not closer

    def _single(self, i: int) -> tuple[str, int, bool]:
        s, n = self.s, self.n
        parts: list[str] = []
        j = i + 1
        while j < n:
            ch = s[j]
            if ch in _SINGLE_QUOTES:
                if j + 1 < n and s[j + 1] in _SINGLE_QUOTES:
                    parts.append("'")
                    j += 2
                    continue
                return "".join(parts), j + 1, True
            parts.append(ch)
            j += 1
        return "".join(parts), n, False

    def _double(self, i: int) -> tuple[str, tuple[Group, ...], int, bool]:
        s, n = self.s, self.n
        parts: list[str] = []
        groups: list[Group] = []
        j = i + 1
        while j < n:
            ch = s[j]
            if ch == "`":
                if j + 1 < n:
                    parts.append(s[j + 1])
                j += 2
                continue
            if ch in _DOUBLE_QUOTES:
                if j + 1 < n and s[j + 1] in _DOUBLE_QUOTES:
                    parts.append('"')
                    j += 2
                    continue
                return "".join(parts), tuple(groups), j + 1, True
            if s.startswith("$(", j):
                group, k, closed = self._group(j + 1)
                groups.append(group)
                parts.append(s[j:k])
                j = k
                if not closed:
                    return "".join(parts), tuple(groups), n, False
                continue
            parts.append(ch)
            j += 1
        return "".join(parts), tuple(groups), n, False

    def _group(self, i: int) -> tuple[Group, int, bool]:
        closer = ")" if self.s[i] == "(" else "}"
        statements, j, closed = _Lexer(self.s, self.depth + 1)._run(i + 1, closer)
        body = self.s[i + 1:j - 1] if closed else self.s[i + 1:j]
        return Group(body, tuple(statements)), j, closed

    def _here_string(self, i: int) -> tuple[str, tuple[Group, ...], int, bool]:
        s, n = self.s, self.n
        quotes = _SINGLE_QUOTES if s[i + 1] in _SINGLE_QUOTES else _DOUBLE_QUOTES
        j = i + 2
        while s[j] in " \t":
            j += 1
        body_start = j + len(_newline_at(s, j))
        j = body_start
        while True:
            line_end = _line_end(s, j)
            if line_end >= n:
                return s[body_start:], (), n, False
            after = line_end + len(_newline_at(s, line_end))
            if after + 1 < n and s[after] in quotes and s[after + 1] == "@":
                body = s[body_start:line_end]
                groups = () if quotes is _SINGLE_QUOTES else _string_groups(body, self.depth + 1)
                return body, groups, after + 2, True
            j = after


def _string_groups(text: str, depth: int) -> tuple[Group, ...]:
    """Subexpressions of an expandable here-string body."""
    groups: list[Group] = []
    lexer = _Lexer(text, depth)
    i = text.find("$(")
    while i >= 0:
        group, j, _closed = lexer._group(i + 1)
        groups.append(group)
        i = text.find("$(", j)
    return tuple(groups)


def _is_literal(token: Token) -> bool:
    return token.quoted or not re.match(r"[A-Za-z.\\&]", token.text)


def _statement(raw: str, separator: str, tokens: list[Token], irregular: bool, malformed: bool) -> Statement:
    """Leading assignments and wrappers are demoted to values so the real program is classified."""
    toks = list(tokens)
    words = [k for k, t in enumerate(toks) if t.kind == "word"]

    def demote(k: int) -> None:
        toks[k] = replace(toks[k], kind="value")

    if words:
        first = toks[words[0]]
        if (
            len(words) > 1 and _VARIABLE_ONLY.match(first.text) and not first.quoted
            and toks[words[1]].text in _ASSIGN_OPS and not toks[words[1]].quoted
        ):
            demote(words[0])
            demote(words[1])
            words = words[2:]
        elif match := _GLUED_ASSIGNMENT.match(first.text):
            if match.group(1):
                toks[words[0]] = replace(first, text=match.group(1))
            else:
                demote(words[0])
                words = words[1:]
        if len(words) == 1 and _is_literal(toks[words[0]]):
            demote(words[0])
            words = []
    while words:
        t = toks[words[0]]
        if t.quoted or not (t.text.lower() in _WRAPPERS or _ENV_ASSIGNMENT.match(t.text)):
            break
        demote(words[0])
        words = words[1:]
    return Statement(raw, separator, tuple(toks), irregular, malformed)


def lex(command: str) -> list[Statement]:
    return _Lexer(command).statements()


def shell_segments(command: str) -> list[str]:
    return [st.raw for st in lex(command)]


def _single_statement(text: str) -> Statement | None:
    statements = lex(text)
    return statements[0] if len(statements) == 1 else None


_EXE_SUFFIXES = (".exe", ".cmd", ".bat", ".ps1", ".com")
_BARE_NAME = re.compile(r"[A-Za-z][\w.+-]*")


def _program(token: str) -> str:
    name = ntpath.basename(token.replace("/", "\\")).lower()
    for suffix in _EXE_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _bare(token: Token) -> bool:
    return not token.quoted and not token.groups and bool(_BARE_NAME.fullmatch(token.text))


def _compare_tokens(stmt: Statement) -> list[str]:
    words = stmt.words
    return [_program(words[0].text)] + [w.text.lower() for w in words[1:]] if words else []


# Risk classification.

_SHELLS = {"powershell", "pwsh", "cmd", "bash", "sh", "zsh"}
_MAX_SHELL_NESTING = 4
_CMD_VARIABLE = re.compile(r"^%.+%$")
_LAUNCHERS = {
    "start", "start-process", "saps", "wsl", "node", "python", "python3", "py", "perl", "ruby", "php",
    "deno", "bun", "npx", "bunx", "uvx", "pnpx", "pipx", "java", "cscript", "wscript", "mshta",
    "rundll32", "regsvr32", "msiexec", "forfiles", "invoke-command", "icm", "invoke-item", "ii",
    "start-job", "sajb", "invoke-expression", "iex", "eval", ".", "source", "exec", "xargs", "ssh",
    "conhost", "explorer", "timeout",
}
_DELETERS = {"rm", "rmdir", "rd", "del", "erase", "remove-item", "ri", "rimraf"}
_NO_PROGRAM_WIDE = _SHELLS | _LAUNCHERS | _DELETERS
_VERSION_OR_HELP = {"--version", "-version", "-v", "-?", "-h", "-help", "--help", "/?"}
_PS_ALIASES = {
    "ep": "executionpolicy", "wd": "workingdirectory", "cwa": "commandwithargs", "if": "inputformat",
    "of": "outputformat", "ec": "encodedcommand", "ea": "encodedarguments", "w": "windowstyle",
    "nol": "nologo", "nop": "noprofile", "noni": "noninteractive", "noe": "noexit",
}
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
_RG_PREPROCESSOR = re.compile(r"^--pre(?:-glob)?(?:=|$)")
_PACKAGE_RUNNERS = {"npx", "bunx", "uvx", "pnpx"}
_PACKAGE_VERBS = {"install", "i", "add", "ci", "update", "upgrade", "sync", "dlx", "exec", "x"}
_JS_PMS = {"npm", "pnpm", "yarn", "bun"}
_PY_PMS = {"pip", "pip3", "uv", "pipx", "poetry", "pdm", "conda", "mamba"}
_OS_PMS = {"winget", "choco", "scoop", "apt", "apt-get", "brew", "cargo", "go", "gem", "dotnet"}
_JS_SCRIPTS = {"test", "lint", "build", "typecheck", "check", "format"}
_PM_VERBS = _PACKAGE_VERBS | _JS_SCRIPTS | {
    "run", "run-script", "list", "ls", "show", "freeze", "download", "wheel", "config", "pip", "version",
    "help", "init", "publish", "pack", "audit", "outdated", "why", "info", "cache", "link", "unlink",
    "rebuild", "prune", "dedupe", "start", "stop", "restart", "serve", "dev", "watch", "create",
    "uninstall", "remove", "rm", "un", "r", "search", "view", "whoami", "login", "logout", "env", "bin",
    "root", "prefix", "doctor", "ping", "set", "get", "delete", "edit", "import", "export", "lock",
    "tree", "venv", "tool", "self", "python",
}


@dataclass(frozen=True)
class _Context:
    protected: tuple[str, ...]
    root: str | None


def _is_prefix(name: str, full: str) -> bool:
    return bool(name) and full.startswith(name)


def _recursive_flag(arg: str) -> bool:
    if arg in ("--recursive", "/s") or (len(arg) > 1 and "-recurse".startswith(arg)):
        return True
    if re.fullmatch(r"(?:/[a-z])+", arg):
        return "/s" in arg
    return arg.startswith("-") and not arg.startswith("--") and len(arg) <= 4 and "r" in arg


def _delete_risk(prog: str, args: list[str], piped: bool) -> bool:
    if prog not in _DELETERS:
        return False
    return prog == "rimraf" or piped or any(_recursive_flag(a) for a in args)


_GIT_VALUED_GLOBALS = {"-c", "-C", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--config-env", "--super-prefix"}
_GIT_EXEC_OPTIONS = ("--upload-pack", "--receive-pack", "--exec", "--open-files-in-pager")
_GIT_CONFIG_READ_FLAGS = {
    "--get", "--get-all", "--get-regexp", "--get-urlmatch", "-l", "--list", "--show-origin", "--show-scope",
    "--name-only", "-z", "--null", "--includes", "--no-includes", "--global", "--system", "--local",
    "--worktree", "--all", "--regexp", "--value", "--fixed-value", "--url", "--type", "--file", "--default",
}
_GIT_CONFIG_VALUED_READ = {"--type", "-t", "-f", "--file", "--blob", "--default"}


def _git_split(args: list[str]) -> tuple[list[str], str, list[str]]:
    i = 0
    while i < len(args):
        if args[i] in _GIT_VALUED_GLOBALS:
            i += 2
        elif args[i].startswith("-"):
            i += 1
        else:
            return args[:i], args[i], args[i + 1:]
    return args, "", []


def _git_config_override(options: list[str]) -> bool:
    i = 0
    while i < len(options):
        a = options[i]
        if a.startswith("--config-env") or (a.startswith("-c") and "=" in a):
            return True
        if a == "-c" and i + 1 < len(options) and "=" in options[i + 1]:
            return True
        i += 2 if a in _GIT_VALUED_GLOBALS else 1
    return False


def _git_runs_helper(sub: str, args: list[str]) -> bool:
    for a in args:
        if a.startswith(_GIT_EXEC_OPTIONS):
            return True
        if sub == "clone" and (a == "-u" or a in ("-c", "--config") or a.startswith("--config=")):
            return True
        if sub == "grep" and a.startswith("-O"):
            return True
    return False


def _git_config_is_read(args: list[str]) -> bool:
    positionals, i = 0, 0
    while i < len(args):
        a = args[i]
        if a in _GIT_CONFIG_VALUED_READ:
            i += 2
            continue
        if a.startswith("-"):
            if a.split("=", 1)[0] not in _GIT_CONFIG_READ_FLAGS:
                return False
        elif positionals == 0 and a in ("get", "list"):
            pass
        elif positionals == 0 and a in ("set", "unset", "edit", "rename-section", "remove-section"):
            return False
        else:
            positionals += 1
        i += 1
    return positionals <= 1


def _git_risk(args: list[str]) -> str | None:
    options, sub, sargs = _git_split(args)
    if _git_config_override(options) or _git_runs_helper(sub, sargs):
        return DYNAMIC
    if sub == "config" and not _git_config_is_read(sargs):
        return DYNAMIC
    low = [a.lower() for a in sargs]
    if sub in ("push", "rebase", "filter-branch", "filter-repo"):
        return GIT
    if sub == "clean":
        return DELETE
    if sub == "reset" and "--hard" in low:
        return GIT
    if sub in ("branch", "tag") and any(a in ("-d", "--delete") for a in low):
        return GIT
    if sub == "branch" and any(a in ("-m", "--move") for a in low):
        return GIT
    if sub in ("checkout", "switch") and any(a in ("-f", "--force", "--discard-changes") for a in low):
        return GIT
    if sub == "checkout" and ("--" in low or "." in low):
        return GIT
    if sub == "restore" and not ("--staged" in low and "--worktree" not in low):
        return GIT
    if sub == "stash" and any(a in ("drop", "clear") for a in low):
        return GIT
    if sub == "commit" and "--amend" in low:
        return GIT
    return None


def _pm_args(args: list[str]) -> list[str]:
    """Args from the package manager's subcommand on; a leading flag's value is skipped unless it is a verb."""
    i = 0
    while i < len(args):
        a = args[i]
        if not a.startswith("-"):
            return args[i:]
        i += 1
        if "=" not in a and i < len(args) and not args[i].startswith("-") and args[i] not in _PM_VERBS:
            i += 1
    return []


def _package_risk(prog: str, args: list[str]) -> bool:
    rest = _pm_args(args)
    sub = rest[0] if rest else ""
    if prog in _PACKAGE_RUNNERS:
        return True
    if prog in _JS_PMS:
        return (prog == "yarn" and not sub) or sub in _PACKAGE_VERBS
    if prog == "pipx" and sub == "run":
        return True
    if prog in _PY_PMS:
        return any(a in ("install", "add", "sync", "upgrade") for a in rest[:2])
    if prog in ("python", "python3", "py") and "-m" in args:
        mod = args[args.index("-m") + 1] if args.index("-m") + 1 < len(args) else ""
        return mod == "ensurepip" or (mod == "pip" and "install" in args)
    if prog in _OS_PMS:
        return any(a in ("install", "upgrade") for a in rest[:2]) or (prog == "dotnet" and sub == "add")
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


def _body(tokens: Sequence[Token]) -> str:
    return " ".join(t.text for t in tokens)


def _nested_body_risk(body: str, ctx: _Context, depth: int, unmodelled: Sequence[str]) -> str | None:
    """Fail closed on a body that reads stdin or uses syntax the lexer does not model for that shell."""
    stripped = body.strip()
    if depth >= _MAX_SHELL_NESTING or not stripped or stripped == "-" or stripped.startswith("- "):
        return DYNAMIC
    if any(mark in body for mark in unmodelled):
        return DYNAMIC
    return _risk_text(body, ctx, depth + 1)


def _powershell_risk(args: Sequence[Token], ctx: _Context, depth: int) -> str | None:
    """Parameter prefixes resolve as PowerShell does, and a bare positional argument is the command."""
    i = 0
    while i < len(args):
        tok = args[i]
        text = tok.text.lower()
        if tok.quoted or not text.startswith(("-", "/")):
            return _nested_body_risk(_body(args[i:]), ctx, depth, _DOUBLE_QUOTES)
        name = text.lstrip("-/")
        name = _PS_ALIASES.get(name, name)
        if _is_prefix(name, "encodedcommand"):
            return DYNAMIC
        if _is_prefix(name, "commandwithargs"):
            return _nested_body_risk(_body(args[i + 1:]), ctx, depth, _DOUBLE_QUOTES)
        if _is_prefix(name, "file"):
            return DYNAMIC if i + 1 < len(args) and args[i + 1].text == "-" else None
        plain = any(_is_prefix(name, flag) for flag in _PS_PLAIN_FLAGS)
        valued = any(_is_prefix(name, flag) for flag in _PS_VALUED_FLAGS)
        if plain == valued:
            return DYNAMIC
        i += 2 if valued else 1
    return DYNAMIC


def _cmd_risk(args: Sequence[Token], ctx: _Context, depth: int) -> str | None:
    for i, tok in enumerate(args):
        text = tok.text.lower()
        if tok.quoted:
            return DYNAMIC
        if text in ("/c", "/k"):
            return _nested_body_risk(_body(args[i + 1:]), ctx, depth, ("^", "%", "!"))
        if text == "/?":
            return None
        if not _CMD_PLAIN_FLAG.match(text):
            return DYNAMIC
    return DYNAMIC


def _posix_shell_risk(args: Sequence[Token], ctx: _Context, depth: int) -> str | None:
    i = 0
    while i < len(args):
        tok = args[i]
        raw = tok.text
        if tok.quoted:
            return None
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
                command = next((a for a in args[i + 1:] if a.quoted or not a.text.startswith(("-", "+"))), None)
                return _nested_body_risk(command.text, ctx, depth, ("`", "\\", "$'")) if command else DYNAMIC
            if "s" in letters or not set(letters) <= _SH_LETTERS:
                return DYNAMIC
            i += 1 if letters[-1] not in "oO" else 2
            continue
        else:
            return DYNAMIC if raw == "-" else None
        i += 1
    return DYNAMIC


def _nested_shell_risk(prog: str, args: Sequence[Token], ctx: _Context, depth: int) -> str | None:
    if len(args) == 1 and args[0].text.lower() in _VERSION_OR_HELP:
        return None
    if prog in ("powershell", "pwsh"):
        return _powershell_risk(args, ctx, depth)
    if prog == "cmd":
        return _cmd_risk(args, ctx, depth)
    return _posix_shell_risk(args, ctx, depth)


def _program_risk(stmt: Statement, ctx: _Context, depth: int, piped: bool) -> str | None:
    words = stmt.words
    if not words:
        return None
    prog = _program(words[0].text)
    args = [w.text.lower() for w in words[1:]]
    if prog in _SHELLS:
        return _nested_shell_risk(prog, words[1:], ctx, depth)
    if prog in ("invoke-expression", "iex", "eval") or _CMD_VARIABLE.match(words[0].text):
        return DYNAMIC
    if _delete_risk(prog, args, piped):
        return DELETE
    if prog == "git":
        return _git_risk([w.text for w in words[1:]])
    if prog == "rg" and any(_RG_PREPROCESSOR.match(a) for a in args):
        return DYNAMIC
    if _package_risk(prog, args):
        return PACKAGE
    if prog in _NETWORK or (prog == "certutil" and "-urlcache" in args):
        return NETWORK
    if _system_risk(prog, args):
        return SYSTEM
    return None


def _group_risk(stmt: Statement, ctx: _Context, depth: int, piped: bool) -> str | None:
    for group in stmt.groups:
        for inner in group.statements:
            if risk := _risk(inner, ctx, depth + 1, piped or inner.piped):
                return risk
    return None


def _risk(stmt: Statement, ctx: _Context, depth: int, piped: bool) -> str | None:
    if depth > _MAX_NESTING:
        return DYNAMIC
    if stmt.stop_parsing or stmt.malformed:
        return DYNAMIC
    if _mentions_protected(stmt, ctx):
        return GATE
    return _group_risk(stmt, ctx, depth, piped) or _program_risk(stmt, ctx, depth, piped)


def _risk_text(text: str, ctx: _Context, depth: int, piped: bool = False) -> str | None:
    if depth > _MAX_NESTING:
        return DYNAMIC
    for stmt in _Lexer(text, depth).statements():
        if risk := _risk(stmt, ctx, depth, piped or stmt.piped):
            return risk
    return None


# Strict mode: an allowlist by syntax, then by program.

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
    "shortlog", "fetch", "reflog", "branch", "remote", "config",
}
_BUILD_SUBS = {
    "cargo": {"build", "test", "check", "clippy", "fmt", "tree"},
    "go": {"build", "test", "vet", "fmt", "list", "env", "version"},
    "dotnet": {"build", "test"},
}
_PY_MODULES = {"pytest", "unittest", "mypy", "ruff", "pyright", "black", "compileall"}


def _routine(stmt: Statement) -> bool:
    if not stmt.plain or stmt.separator == "&":
        return False
    words = stmt.words
    if not words:
        return True
    if not _bare(words[0]):
        return False
    prog = _program(words[0].text)
    args = [w.text.lower() for w in words[1:]]
    if args in (["--version"], ["-v"], ["-V"], ["--help"]):
        return True
    if prog in _ROUTINE:
        return True
    if prog == "git":
        raw_args = [w.text for w in words[1:]]
        return _git_split(raw_args)[1] in _GIT_ROUTINE and _git_risk(raw_args) is None
    if prog in ("python", "python3", "py"):
        return "-m" in args and args.index("-m") + 1 < len(args) and args[args.index("-m") + 1] in _PY_MODULES
    if prog in _JS_PMS:
        rest = _pm_args(args)
        sub = rest[0] if rest else ""
        return sub in _JS_SCRIPTS or (sub == "run" and len(rest) > 1 and rest[1] in _JS_SCRIPTS)
    return prog in _BUILD_SUBS and bool(args) and args[0] in _BUILD_SUBS[prog]


# Rules.

_WORDLIKE = re.compile(r"^[a-z][a-z0-9-]*$")


def _program_fits(want: Statement, have: Statement) -> bool:
    """A rule written with a bare program name covers only bare invocations of it."""
    return not _bare(want.words[0]) or _bare(have.words[0])


def rule_matches(rule: str, segment: str | Statement) -> bool:
    try:
        want = _single_statement(rule)
        have = segment if isinstance(segment, Statement) else _single_statement(segment)
    except TooComplex:
        return False
    if want is None or have is None or not want.words or not have.words or not have.plain:
        return False
    wt, ht = _compare_tokens(want), _compare_tokens(have)
    if wt[-1] == "*":
        return ht[: len(wt) - 1] == wt[:-1] and _program_fits(want, have)
    return ht == wt and _program_fits(want, have)


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
    try:
        stmt = _single_statement(rule)
    except TooComplex:
        return False
    if stmt is None or not stmt.plain or not stmt.words:
        return False
    tokens = _compare_tokens(stmt)
    if tokens[0] in ("*", "") or tokens[0].startswith("$"):
        return False
    anchored = any(not t.startswith(("-", "/")) for t in tokens[1:-1])
    if tokens[-1] == "*" and not anchored:
        return tokens[0] not in _NO_PROGRAM_WIDE and _bare(stmt.words[0])
    return True


def _similar_rule(stmt: Statement, category: str) -> str | None:
    """Family rule for a plain '<program> <word> ...' statement, else the exact command, else nothing."""
    words = stmt.words
    if not words or not stmt.plain:
        return None
    prog = _program(words[0].text)
    if words[0].text.startswith("$") or prog == ".":
        return None
    if stmt.piped and prog in _DELETERS:
        return None
    tokens = _compare_tokens(stmt)
    if tokens[-1] == "*":
        return None
    if len(words) > 1 and _bare(words[0]):
        if not words[1].quoted and _WORDLIKE.match(tokens[1]):
            return f"{prog} {tokens[1]} *"
        if not tokens[1].startswith(("-", "/")) and prog not in _NO_PROGRAM_WIDE and category != DELETE:
            return f"{prog} *"
    return stmt.raw


def similar_rule_for(segment: str) -> str | None:
    try:
        stmt = _single_statement(segment)
    except TooComplex:
        return None
    return _similar_rule(stmt, "") if stmt else None


def _shell_grants(grants: Sequence[str]) -> list[str]:
    return [g for g in grants if valid_rule(g) and not g.startswith(("file ", "mcp "))]


# Paths.

_DEVICE_PREFIX = re.compile(r"^\\\\[?.]\\(?:unc\\)?", re.I)
_LOCAL_ADMIN_SHARE = re.compile(r"^\\\\(?:localhost|127\.0\.0\.1|::1|\[::1\])\\([a-z])\$(?=[\\/]|$)", re.I)


def _canonical(path: str) -> str:
    """Windows device, long-path and local admin-share spellings reduced to the plain drive form."""
    p = path.replace("/", "\\")
    if match := _DEVICE_PREFIX.match(p):
        rest = p[match.end():]
        p = rest if re.match(r"^[a-z]:", rest, re.I) else "\\\\" + rest
    if match := _LOCAL_ADMIN_SHARE.match(p):
        p = match.group(1) + ":" + (p[match.end():] or "\\")
    return p


def _pathmod(*paths: str):
    windows = any(re.match(r"^[A-Za-z]:", p) or "\\" in p for p in paths if p)
    return ntpath if windows else posixpath


def path_key(path: str) -> str:
    pm = _pathmod(path)
    return pm.normcase(pm.normpath(_canonical(path) if pm is ntpath else path))


def _under(norm_path: str, norm_dir: str, pm) -> bool:
    return norm_path == norm_dir or norm_path.startswith(norm_dir.rstrip("\\/") + pm.sep)


def _normalized(paths: Iterable[str], pm) -> list[str]:
    return [pm.normcase(pm.normpath(_canonical(p) if pm is ntpath else p)) for p in paths if p]


def _resolve(text: str, root: str | None, pm) -> str | None:
    text = _canonical(text) if pm is ntpath else text
    if pm.isabs(text):
        return pm.normcase(pm.normpath(text))
    return pm.normcase(pm.normpath(pm.join(root, text))) if root else None


def _mentions_protected(stmt: Statement, ctx: _Context) -> bool:
    """A protected path written in full, or any argument or redirection target that resolves to one."""
    if not ctx.protected:
        return False
    pm = _pathmod(ctx.root or "", *ctx.protected)
    wants = _normalized(ctx.protected, pm)
    flat = pm.normcase(stmt.raw)
    boundary = r"(?=$|[\\/\s" + re.escape(_QUOTES) + "])"
    if any(re.search(re.escape(w) + boundary, flat) for w in wants):
        return True
    for tok in stmt.tokens:
        if tok.kind not in ("word", "value", "redirect") or not tok.text:
            continue
        resolved = _resolve(tok.text, ctx.root, pm)
        if resolved and any(_under(resolved, w, pm) for w in wants):
            return True
    return False


def _same_file_as_any(full: str, protected: Sequence[str]) -> bool:
    """Identity check for targets that exist: catches hard links and spellings normalisation misses."""
    node = full
    while True:
        for p in protected:
            try:
                if os.path.samefile(node, p):
                    return True
            except (OSError, ValueError):
                continue
        parent = os.path.dirname(node)
        if parent == node:
            return False
        node = parent


_PROFILE_ROOT = re.compile(r"^(?:[a-z]:\\users\\[^\\]+|/(?:home|users)/[^/]+|/root)$")


def _root_like(folder: str, pm) -> bool:
    return pm.dirname(folder) == folder or bool(_PROFILE_ROOT.match(pm.normcase(folder)))


def _file_rule_allows(rule: str, norm_full: str, pm) -> bool:
    target = _file_rule_target(rule) if rule.startswith("file ") else None
    if target is None:
        return False
    path, folder = target
    want = pm.normcase(pm.normpath(_canonical(path) if pm is ntpath else path))
    return pm.dirname(norm_full) == want if folder else norm_full == want


def _offered_file_rule(full: str, pm) -> str:
    parent = pm.dirname(full)
    return f"file {full}" if _root_like(parent, pm) else f"file {pm.join(parent, '*')}"


# Verdicts.


def _statement_verdict(stmt: Statement, mode: str, rules: list[str], ctx: _Context, command: str) -> Verdict | None:
    if stmt.stop_parsing or stmt.malformed:
        return Verdict(ASK, "shell", command, DYNAMIC)
    if _mentions_protected(stmt, ctx):
        return Verdict(ASK, "shell", command, SYSTEM)
    covered = stmt.plain and any(rule_matches(r, stmt) for r in rules)
    category = _group_risk(stmt, ctx, 0, stmt.piped)
    if not category and not covered:
        category = _program_risk(stmt, ctx, 0, stmt.piped)
    if category == GATE:
        return Verdict(ASK, "shell", command, SYSTEM)
    if not category and mode == "strict" and not covered and not _routine(stmt):
        category = UNLISTED
    if category:
        return Verdict(ASK, "shell", command, category, _similar_rule(stmt, category))
    return None


def command_verdict(
    command: str, mode: str, grants: Sequence[str] = (), truncated: bool = False, *,
    protected: Sequence[str] = (), session_root: str | None = None,
) -> Verdict:
    mode = normalize_mode(mode)
    if mode == "trusted":
        return Verdict(ALLOW, "shell", command)
    if truncated or len(command) > MAX_COMMAND_CHARS:
        return Verdict(ASK, "shell", command, TRUNCATED)
    ctx = _Context(tuple(protected), session_root)
    rules = _shell_grants(grants)
    unlisted: Verdict | None = None
    try:
        for stmt in lex(command):
            verdict = _statement_verdict(stmt, mode, rules, ctx, command)
            if verdict and verdict.category != UNLISTED:
                return verdict
            unlisted = unlisted or verdict
    except TooComplex:
        return Verdict(ASK, "shell", command, TRUNCATED)
    return unlisted or Verdict(ALLOW, "shell", command)


def file_verdict(
    path: str, mode: str, session_root: str | None, grants: Sequence[str] = (), *,
    protected: Sequence[str] = (),
) -> Verdict:
    mode = normalize_mode(mode)
    if mode == "trusted":
        return Verdict(ALLOW, "file", path)
    pm = _pathmod(path, session_root or "")
    given = _canonical(path) if pm is ntpath else path
    if not path or not (session_root or pm.isabs(given)):
        return Verdict(ASK, "file", path, OUTSIDE)
    full = pm.normpath(given if pm.isabs(given) else pm.join(session_root or "", given))
    if pm is os.path:
        full = os.path.realpath(full)
    norm_full = pm.normcase(full)
    if any(_under(norm_full, w, pm) for w in _normalized(protected, pm)):
        return Verdict(ASK, "file", path, GATE)
    if pm is os.path and _same_file_as_any(full, protected):
        return Verdict(ASK, "file", path, GATE)
    if not session_root:
        return Verdict(ASK, "file", path, OUTSIDE)
    root = pm.normpath(session_root)
    if pm is os.path:
        root = os.path.realpath(root)
    norm_root = pm.normcase(root)
    granted = any(_file_rule_allows(rule, norm_full, pm) for rule in grants)
    if _under(norm_full, norm_root, pm):
        if mode == "strict" and not granted and _under(norm_full, pm.join(norm_root, ".git"), pm):
            return Verdict(ASK, "file", path, GITDIR, _offered_file_rule(full, pm))
        return Verdict(ALLOW, "file", path)
    if granted:
        return Verdict(ALLOW, "file", path)
    return Verdict(ASK, "file", path, OUTSIDE, _offered_file_rule(full, pm))


# Tool routing.

_SHELL_TOOLS = {"run_terminal_command", "bash", "shell", "powershell", "run_command", "execute_command"}
_FILE_WORDS = (
    "write", "edit", "replace", "patch", "delete", "create", "move", "rename", "remove",
    "copy", "append", "save", "apply_diff", "applydiff", "mkdir", "touch", "unlink",
)
_PATH_KEYS = {
    "path", "file", "filename", "filepath", "target", "targetfile", "source", "src", "dest",
    "destination", "new", "old", "notebookpath", "directory", "dir", "folder", "to", "from", "dst",
    "paths", "files", "uri",
}
_PATH_KEY_SUFFIXES = ("path", "file", "filename", "dir", "directory", "folder", "notebook", "uri")
_CONTENT_KEY_SUFFIXES = ("string", "content", "contents", "text", "data", "body", "code", "pattern", "replacement")
_ABSOLUTE_WINDOWS = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)[^\r\n]*$")
_ABSOLUTE_POSIX = re.compile(r"^/[^\r\n]*$")
_FILE_URI = re.compile(r"^file://(?P<host>[^/]*)/(?P<path>.*)$", re.I)


def _key_name(key: str) -> str:
    return re.sub(r"[^a-z]", "", key.lower())


def _uri_path(value: str) -> str:
    match = _FILE_URI.match(value)
    if not match:
        return value
    host, path = match.group("host"), unquote(match.group("path"))
    if host in ("", "localhost"):
        return path if re.match(r"^[A-Za-z]:", path) else "/" + path
    return "\\\\" + host + "\\" + path.replace("/", "\\")


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
        return [_uri_path(value)]
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
        if normalize_mode(mode) == "trusted":
            return Verdict(ALLOW, "file", name)
        posix = bool(session_root) and _pathmod(session_root) is posixpath
        targets = _file_targets(tin, posix)
        if not targets:
            return Verdict(ASK, "file", name, NO_TARGET)
        verdicts = (file_verdict(p, mode, session_root, grants, protected=protected) for p in targets)
        return next((v for v in verdicts if v.action != ALLOW), Verdict(ALLOW, "file", targets[0]))
    return Verdict(ALLOW, "tool", name)
