# File Tree + Mobile Composer — Design Spec

**Date:** 2026-07-10  
**Status:** Approved  
**Goal:** Add a project file tree (browse, preview, edit/save, insert path) via rail tabs, and fix mobile Safari typing (composer height + no zoom).

---

## 1. Problem

1. Remote hub users cannot browse or edit project files from the phone/desktop UI; they only chat.
2. On iPhone Safari, the composer shows ~1–2 lines with internal scroll, so long drafts are hard to review.
3. Focusing search (and other inputs under 16px) zooms the Safari viewport; zoom should not happen on the mobile app shell.

---

## 2. Decisions (locked)

| Decision | Choice |
|---|---|
| Rail layout | Tabs: **Sessions** \| **Files** (segment control under brand/New) |
| Tree root | Selected session’s **project cwd** |
| File open | Main pane switches to file mode (chat transcript hidden) |
| v1 file ops | List dirs, read text, edit/save text, insert path into composer |
| Out of scope v1 | Create/rename/delete, binary preview, syntax highlight, live FS watch, multi-tab editors |
| Path sandbox | All FS ops resolve under `config.projects_root`; reject escape/`..`/outside root |
| Mobile zoom | `maximum-scale=1, user-scalable=no` + inputs ≥16px on mobile |
| Mobile composer | Auto-grow to a large fraction of visual viewport; scroll only after max |

---

## 3. Architecture

```
Browser
  rail: Sessions | Files
  main: chat mode | file mode
        │
        │ REST
        ▼
Hub server
  GET  /api/fs/list
  GET  /api/fs/read
  PUT  /api/fs/write
        │
        ▼
hub/fs_browser.py  (sandbox + list/read/write)
        │
        ▼
Project files under projects_root / session cwd
```

ACP `acp_fs.py` remains agent-only. UI FS uses a separate `fs_browser` module.

---

## 4. UI / UX

### 4.1 Rail tabs

```
[ brand              New ]
[ Sessions | Files       ]  ← tablist
[ search / filter        ]
[ list body…             ]
[ version badge          ]
```

- **Sessions** tab: existing session list + session search.
- **Files** tab: filter input + file tree rows.
- No session selected: Files empty state — “Open a session to browse its project.”
- Session changes: reset tree cache; if open file’s root no longer matches, exit file mode (confirm if dirty).

### 4.2 File tree rows

Session-like density (grid row, mono, hover/active):

- Folder: expand/collapse chevron + name; lazy-load children on first expand.
- File: name + optional size meta; click opens in file mode.
- Indent by depth; active row when that path is open in main.

### 4.3 File mode (main)

```
[ ← Chat ]  relative/path.ext     [ Insert path ] [ Save ]
[ dirty • if edited ]
[ mono textarea (full height) ]
```

- **← Chat**: return to transcript/composer; confirm if dirty.
- **Insert path**: append path into composer (prefer project-relative with `/` separators; fall back to absolute if needed), switch to chat mode, focus composer.
- **Save**: PUT write; clear dirty on success.
- Unsaved: confirm before other file open, session switch, or ← Chat.
- Binary / too large / non-text: show error, not editable.

### 4.4 Mobile composer

- `autoGrow()` sets height from `scrollHeight`, max ≈ 40% of `visualViewport.height` (floor ~6 lines, cap reasonable).
- `overflow-y: auto` only when at max height.
- Re-run grow on input and `visualViewport` resize.
- Transcript flex-shrinks; draft stays visible above keyboard.

### 4.5 Mobile zoom (Safari iPhone)

1. Viewport meta:  
   `width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no, viewport-fit=cover`
2. `@media (max-width: 899px)`: `input`, `textarea`, `.input`, `.composer-input` → `font-size: 16px` (prevents focus-zoom).

---

## 5. API

All require same hub auth as other `/api/*` routes.

### `GET /api/fs/list?root=<abs-cwd>&path=<rel>`

- `root`: absolute session cwd (must resolve under `projects_root`).
- `path`: relative to root (default `""` = root itself).
- Response: `{ "root", "path", "entries": [ { "name", "type": "file"|"dir", "size": number|null } ] }`
- Entries sorted: dirs first, then files; case-insensitive name.
- 400 if invalid/escape; 404 if path missing; 400 if not a directory.

### `GET /api/fs/read?root=<abs-cwd>&path=<rel>`

- Response: `{ "root", "path", "content", "size", "truncated": bool }`
- Max read size: **1_500_000** bytes; if larger, return 413 or truncated flag with partial content (prefer **413** for simplicity).
- Decode UTF-8 with `errors="replace"`; reject if path is directory.
- Skip null-byte-heavy binaries: if `\0` in first 8KB, return 415 unsupported media.

### `PUT /api/fs/write`

Body JSON: `{ "root", "path", "content" }`

- Write only under sandboxed root; create parent dirs not required for v1 (file must exist **or** allow write to new path under existing parent — **allow write if parent dir exists**).
- Atomic write pattern (temp + replace), same spirit as `acp_fs.write_text_file`.
- Max content length: **1_500_000** chars/bytes.
- Response: `{ "root", "path", "size" }`

---

## 6. Sandbox rules (`hub/fs_browser.py`)

1. Resolve `projects_root` and `root` with `Path.resolve()`.
2. `root` must be under `projects_root` (or equal).
3. Join `root / path` (reject absolute `path`, reject `..` in parts).
4. Resolve target; must stay under `root` (and thus under `projects_root`).
5. On Windows, compare casefold for under-check.
6. Do not follow symlink out of root: after resolve, re-check under root.

Errors: raise typed `FsBrowserError` with `status` and `message` for HTTP mapping.

---

## 7. Frontend state

```js
railTab: "sessions" | "files"
mainMode: "chat" | "file"
fs: {
  root: string,           // cwd
  filter: string,
  expanded: Set<string>,  // rel paths
  cache: Map<string, Entry[]>,
  openPath: string|null,  // rel
  content: string,
  baseline: string,       // last loaded/saved
  dirty: boolean,
  loading: boolean,
  error: string|null,
  saving: boolean,
}
```

- Persist `railTab` in `sessionStorage` key `grh.railTab`.
- Files filter is client-side on currently rendered names (simple).

---

## 8. Testing

| Layer | Cases |
|---|---|
| Unit `fs_browser` | list/read/write happy path; `..` escape; absolute path reject; outside projects_root; missing path; write size cap; binary detect |
| UI (manual/smoke) | tabs switch; expand folder; open/edit/save; insert path; dirty confirm; mobile font 16px; composer multi-line |

---

## 9. Security notes

- FS write over Tailscale is high impact; sandbox is mandatory.
- Do not log file contents.
- Existing `hub_token` auth applies unchanged.

---

## 10. Non-goals (v1)

- File/folder create, rename, delete  
- Diff, syntax highlighting, download  
- Auto-refresh tree on disk change  
- Editing files outside `projects_root`  
