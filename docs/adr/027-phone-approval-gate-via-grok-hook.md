# ADR-027: Approve risky agent actions from the phone via a hub-owned Grok hook

- **Status:** Accepted
- **Date:** 2026-10-31
- **Deciders:** project owner, Grok Remote Hub session
- **Tags:** security, permissions, hooks, approvals, mobile

## Context

The agent runs with `--always-approve` (ADR-023), so every tool call runs
unasked. Remote control needs Claude-style approvals: the phone decides on
risky actions. Probes against `grok agent serve` (2026-10-31) showed:

1. The agent never sends `session/request_permission`, with or without
   `--always-approve`; serve sessions report `permissionMode=bypassPermissions`.
   Relaying ACP permission requests is therefore not possible.
2. Shell commands and file writes are executed by the client (the hub) via
   `terminal/create` and `fs/write_text_file`.
3. Grok supports `PreToolUse` HTTP hooks to loopback, loaded per process with
   `grok agent --plugin-dir`. A hook held a shell call for 150 s; its `deny`
   blocked execution (no `terminal/create` reached the client).
4. Hooks **fail open** on timeout, crash or bad output; default timeout 10 s.
5. Built-in web search did not pass through the hook (runs server-side).

## Decision

1. **Gate point.** The hub writes a plugin to `data/grok-approval-gate/hooks/hooks.json`
   with one `PreToolUse` HTTP hook (no matcher, timeout 330 s) to
   `http://127.0.0.1:<bind_port>/internal/pre-tool-use?key=<data/hook.secret>`,
   and spawns the agent with `--plugin-dir data/grok-approval-gate` (serve and leader).
   The key persists so an agent kept across hub restarts still matches.
2. **Endpoint.** Loopback peers only, key compared in constant time, exempt
   from the browser token like the preview capability path (ADR-026). Replies
   `defer` (policy has no objection), `allow` (user approved) or `deny` + reason.
3. **Policy** (`hub/approval_policy.py`, pure). Modes per session, default
   `hub.approval_mode = "balanced"`:
   - *balanced*: shell asks when any segment is risky (recursive delete, git
     push/reset/force/clean, package install, network transfer, system change,
     dynamic or encoded execution, truncated input); file-changing tools ask
     only outside the session folder; other tools run.
   - *strict*: shell asks unless every segment is routine (status, diff, list,
     search, tests, builds); file rules as balanced; MCP tools ask.
   - *trusted*: never asks (today's behaviour).
4. **Approvals broker** (`hub/approvals.py`). Fans out `approval_request` to all
   clients, waits for `approval_answer`, denies at 300 s (before the hook
   timeout, so the gate never fails open by waiting). "Allow similar" saves a
   rule such as `git push *` per project in `data/approval-rules.json`, which
   the user can read and edit. Pending requests ride on `status` for reconnects.
5. **Executor backstop.** `terminal/create` and `fs/write_text_file` re-check:
   the policy allows it, or the broker approved the same session + command or
   path in the last 10 minutes. Otherwise the hub refuses to execute. This
   closes the fail-open gap for the two actions the hub runs itself.
6. **Mode switch** from the UI (`set_approval_mode`), held in memory; a hub
   restart returns sessions to the configured default.

## Alternatives considered

- **Relay `session/request_permission`:** the agent never sends it in serve mode.
- **Gate only at the executor:** misses MCP tools and sees no tool context.
- **Drop `--always-approve`:** no effect; serve already reports bypass.

## Consequences

### Positive

- Phone approves or denies risky steps; shell, file writes and MCP tools are
  covered. The desktop CLI is untouched (plugin is per agent process).

### Negative

- Web search and other server-side tools are not gated.
- Command patterns catch mistakes, not a determined attacker: in balanced mode
  `python script.py` runs and may do anything. The folder boundary for file
  writes is the reliable line; strict mode is for untrusted work.
- A Grok hook-format change would silently fail open; the executor backstop
  still covers shell and file writes.

## Validation

- `tests/test_approval_policy.py`, `tests/test_approvals.py`: policy per mode,
  broker timeout and grants, endpoint auth, executor backstop.
- Live: a hub turn that pushes or installs shows the card; Allow runs it, Deny
  and timeout block it.

## Related

- ADR-023 always-approve remote risk (risk reduced by this ADR)
- ADR-026 Host/Origin guard and capability paths

## Notes (2026-10-31)

Review hardening: approval payloads show bidi and zero-width characters as
visible `<U+XXXX>` markers so a command cannot be disguised on the card, and
carry `serverNow` so the phone's countdown ignores its own clock skew.

## Amendment (2026-11-02): rule scope, PowerShell grammar, protected paths

A security review of the gate found that the classifier parsed POSIX-style
while every command runs under PowerShell (`hub/acp_terminal.py`, since
8b66cb4), that "Allow similar" rules were broader than the card text, and that
the gate's own state could be rewritten without a card. Each claim was
reproduced through the hub's spawn path before the fix. Decisions:

1. **Rule scope follows the card.** `Allow similar` offers `<program> <word> *`
   only for a plain segment whose second token is a word. A nested shell, a
   flag-led command (`git -C x push`, `python -m pip install x`, `rm -rf build`)
   or a segment with a subexpression, script block or backtick gets the exact
   command instead; a segment ending in `*` gets no rule. `file <dir>\*` covers
   that folder only, not its subfolders. A write under a drive root or a user
   profile root offers `file <that file>` only. Rules that match everything
   (`*`, `file *`, `sudo *`, wrapper-only, empty) are rejected when saved and
   ignored when read. Cost: users who used to approve a whole family in one
   tap now approve flag-led commands one exact command at a time.
2. **PowerShell is the grammar.** The splitter honours backtick escapes, keeps
   `$( )`, `@( )`, `( )` and `{ }` bodies with their segment, and scans every
   body for risk in all modes (a delete cmdlet inside a pipe-fed block counts
   as DELETE). Strict treats any segment with grouping, a backtick or a call
   operator as not routine. Shell flags are resolved by prefix the way
   PowerShell does (`-com`, `-e`, `/Command`), a bare positional is the command
   (Windows PowerShell 5.1), bundled POSIX short flags are expanded, and a shell
   whose flags are not recognised is DYNAMIC rather than ignored, as is nesting
   deeper than four shells. `git -c key=value`, `git --config-env`, `rg --pre`
   and `rg --pre-glob` are DYNAMIC in every mode; `git -C path` is told apart
   from `-c key=value` by the `=`. Balanced also catches Remove-Item aliases
   with `-Recurse` prefixes (`rd -r`, `del -Recurse`), pipe-fed deletes, and
   `git checkout|switch -f|--force|--discard-changes`.
3. **The gate protects itself.** The broker derives protected paths from the
   rules file: its data dir (rules, hook secret, plugin dir) and `config.toml`
   beside it. File writes there always ask (category GATE) whatever the session
   root, no rule can allow them, and shell segments naming them, by full path
   or by a token that resolves to them against the session root, ask as SYSTEM
   without a rule offer. `server.py` passes nothing; `protected_paths` exists
   for tests and future config.
4. **Deadline and delivery.** The 300 s deadline is anchored to the event's
   arrival and broadcasts run as background tasks (resolved ordered after its
   request), so a stalled client socket can no longer push the hub past Grok's
   330 s fail-open timeout.
5. **Hook endpoint trust.** Besides the loopback peer and the key, the handler
   requires a loopback `Host` and rejects any `Tailscale-User-*` or
   `X-Forwarded-*` header, since Tailscale Serve proxies tailnet requests from
   127.0.0.1. A session root learned only from the event's `cwd` still applies
   saved rules but never offers a project grant.
6. **File tools.** For tools whose name suggests a file change, every string
   under a path-like key (path, file, source, destination, target, new, old and
   `*_path` style keys) or that is itself an absolute path is checked, and the
   first non-allow verdict wins. Content-like keys (`old_string`, `content`)
   are skipped.

Known limits, unchanged in spirit from the Negative consequences above: in
Balanced `python script.py` and `python -c` still run; variable-fed deletes
(`Remove-Item $files`) and `cd x; Set-Content config.toml` across segments are
not resolved; the user profile root is recognised by path shape
(`<drive>:\Users\<name>`, `/home/<name>`), not by the environment.

## Amendment 2 (2026-11-02): a lexer, and fail closed where it stops

An adversarial review of the first amendment showed that a classifier which
re-splits text with regular expressions always has one more gap (comments,
Unicode quotes, a lone CR, here-strings, assignments, redirections). The policy
now lexes PowerShell once (`_Lexer` in `hub/approval_policy.py`) and classifies
tokens. Two properties replace grammar chasing:

1. **Model what agents really emit.** `#` and `<# #>` comments, `@' '@` and
   `@" "@` here-strings (the latter expands `$( )`), U+2018 to U+201B as `'`
   and U+201C to U+201E as `"`, a lone CR as a newline, backtick-newline as a
   continuation, backticks inside words dropped (``g`it`` is `git`), quoted
   fragments joined (`pu"sh"` is `push`), U+2013 to U+2015 as `-`, `-Name:value`
   split, `n>&m` merges and `n>` redirections (a `>` glued to the end of an
   argument is literal, as PowerShell treats it), and leading `$x =`, `$x +=`,
   `[type]$x =` and glued `$x=` assignments demoted so the real program is seen.
2. **Fail closed on what is not modelled, as DYNAMIC, in both modes:** `--%`,
   unterminated strings, groups, here-strings and comments, cmd bodies holding
   `^`, `%` or `!` (and a `%var%` program), POSIX `-c` bodies holding a
   backtick, a backslash or `$'`, nested `-Command` bodies holding a `"` (5.1
   passes embedded quotes to the child unescaped), `-Command -` (stdin), more
   than four nested shells, and anything over `MAX_COMMAND_CHARS` (4096) or
   eight nested groups, which both report TRUNCATED. The executor backstop uses
   the same path, so it is bounded too.
3. **Strict is an allowlist by syntax first.** A statement is routine only if
   its program is a bare name (no path, `.\`, `&`, dot-source or `$var`), it has
   no file redirection (`2>&1` merges are fine), no groups, escapes or
   here-strings, no call operator, and the program is on the list. The
   `--version`/`--help` shortcut needs a bare name too. A concrete risk found
   anywhere in the command is reported ahead of UNLISTED.
4. **Rules cover less.** A saved rule covers a statement only when it is plain
   (no groups, escapes, here-strings or redirection) and, if the rule names a
   bare program, only bare invocations. Groups are scanned first and always.
   Program-wide rules (`<prog> *` with nothing but flags before the `*`) are
   invalid for shells, launchers, deleters, `iex`, `.` and `$`-programs; no rule
   is offered for a pipe-fed delete, a `$var` program, a dot-source, or a
   statement that a rule could never cover.
5. **Protected paths resolve more spellings.** `\\?\`, `\\.\`, `\\?\UNC\` and
   the local admin share (`\\localhost\D$`) reduce to the drive form before
   comparison; redirection targets and `-Name:value` values resolve like
   arguments; nested shell bodies are checked too. When the target exists,
   `os.path.samefile` against each protected entry (the rules file is now one)
   catches hard links and spellings normalisation misses. This and the
   existing `realpath` are the module's only filesystem calls, both guarded by
   the host path module.
6. **git and package managers.** `--upload-pack`, `--receive-pack`, `--exec`,
   `grep -O`/`--open-files-in-pager`, `clone -c/-u/--config`, and every
   `git config` write (reads such as `--get`, `-l`, `get`, `list` and a single
   key stay routine) are DYNAMIC; valued global flags (`--git-dir x`) no longer
   hide the subcommand; `-m` is a rename only for `branch`. Package manager
   subcommands are found past leading flags (`pnpm --filter web add`). In
   Strict, writes under `<root>\.git\` ask (GITDIR).
7. **File tools.** `to`, `from`, `dst`, `paths`, `files` and `uri` (with
   `file://` decoded) are path keys; `copy`, `append`, `save`, `apply_diff`,
   `mkdir`, `touch` and `unlink` are file words; a file-word tool with no
   recognisable target asks (NO_TARGET) rather than running.
8. **Broker.** Each broadcast is bounded by `publish_timeout` (10 s) so a hung
   `approval_request` publish cannot hold back `approval_resolved`; the
   arrival-anchored deadline is pinned by a fake-clock test.

Known limits recorded for the next reviewer: wildcards, 8.3 short names and
`$var` paths in shell mentions of gate files are not resolved (the file-write
backstop still catches the write itself); the hub's own source (`hub\*.py`,
`.venv`) is not protected when the session root is the hub repo; POSIX
`/bin/sh` semantics beyond the fail-closed marks are not modelled; a delete
fed from a group or variable (`Remove-Item (gci -r x)`, `Remove-Item $files`)
is not DELETE; `cd x; Set-Content config.toml` across statements is not
resolved. A tool whose name holds a file word but carries no path (a hypothetical
`todo_write`) now asks every time; if Grok gains such a tool, exempt it by name.