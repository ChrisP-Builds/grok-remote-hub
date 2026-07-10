# File Tree + Mobile Composer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rail Sessions|Files tabs with sandboxed project file tree (list/read/edit/save/insert path), plus mobile Safari no-zoom and multi-line composer growth.

**Architecture:** New `hub/fs_browser.py` for path sandbox + list/read/write; REST routes on `hub/server.py`; static UI rail tabs + file mode in `index.html` / `app.css` / `app.js`.

**Tech Stack:** Python 3.11+ / aiohttp / pytest; static HTML/CSS/JS (no build step).

## Global Constraints

- Paths must never escape `config.projects_root` or the session `root` cwd.
- Max file read/write size: 1_500_000 bytes.
- Binary (NUL in first 8KB): reject read with 415.
- Mobile: viewport `maximum-scale=1, user-scalable=no`; inputs ≥16px under 899px.
- Composer auto-grow max ≈ 40% of visualViewport height.
- Do not commit unrelated dirty files; only stage files for this feature.
- No Co-Authored-By; match existing code style (type hints, aiohttp patterns).
- Never use em dashes in user-facing copy or commit messages.

## File map

| File | Role |
|---|---|
| `hub/fs_browser.py` | Sandbox resolve, list_dir, read_text, write_text |
| `tests/test_fs_browser.py` | Unit tests |
| `hub/server.py` | Wire GET list/read, PUT write |
| `static/index.html` | Rail tabs, file list host, file mode chrome |
| `static/app.css` | Tabs, tree rows, file mode, mobile 16px, composer |
| `static/app.js` | Tab state, tree fetch/render, file mode, autoGrow, dirty |

---

### Task 1: `fs_browser` module + unit tests

**Files:**
- Create: `hub/fs_browser.py`
- Create: `tests/test_fs_browser.py`

**Interfaces:**
- Produces:
  - `class FsBrowserError(Exception)` with `.status: int` and `.message: str`
  - `def resolve_sandbox(projects_root: Path, root: str | Path, rel: str = "") -> Path`
  - `def list_dir(projects_root: Path, root: str | Path, rel: str = "") -> dict`
  - `def read_text(projects_root: Path, root: str | Path, rel: str, *, max_bytes: int = 1_500_000) -> dict`
  - `def write_text(projects_root: Path, root: str | Path, rel: str, content: str, *, max_bytes: int = 1_500_000) -> dict`

- [ ] **Step 1: Write tests** in `tests/test_fs_browser.py` covering:
  - list root entries (file + dir), dirs first sort
  - read text content
  - write updates content
  - `..` in rel raises 400
  - absolute rel raises 400
  - root outside projects_root raises 400
  - missing path raises 404
  - list on file raises 400
  - binary NUL raises 415
  - oversize read raises 413
  - oversize write raises 413

- [ ] **Step 2: Implement** `hub/fs_browser.py` to pass tests (Windows-safe under-check via `Path.resolve` + `relative_to` / casefold).

- [ ] **Step 3: Run** `python -m pytest tests/test_fs_browser.py -v` — all pass.

- [ ] **Step 4: Commit** only these two files: `feat: add sandboxed fs_browser list/read/write`

---

### Task 2: HTTP routes on hub server

**Files:**
- Modify: `hub/server.py`
- Test: optional thin tests or rely on unit tests + manual; prefer adding `tests/test_fs_api.py` only if easy with aiohttp test utils — otherwise unit tests sufficient.

**Interfaces:**
- Consumes: `list_dir`, `read_text`, `write_text`, `FsBrowserError` from `hub.fs_browser`
- Produces routes:
  - `GET /api/fs/list?root=&path=`
  - `GET /api/fs/read?root=&path=`
  - `PUT /api/fs/write` JSON body `{root, path, content}`

- [ ] **Step 1:** Import fs_browser; add three handlers mapping `FsBrowserError` → `web.json_response({"error": message}, status=status)`.
- [ ] **Step 2:** Register routes next to projects routes in `build_app`.
- [ ] **Step 3:** Smoke with pytest unit still green; commit: `feat: expose /api/fs list read write endpoints`

---

### Task 3: Mobile zoom + composer grow (can ship before tree UI)

**Files:**
- Modify: `static/index.html` (viewport meta)
- Modify: `static/app.css` (16px inputs mobile; composer overflow)
- Modify: `static/app.js` (`autoGrow` using visualViewport)

- [ ] **Step 1:** Viewport content becomes:  
  `width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no, viewport-fit=cover`
- [ ] **Step 2:** In `@media (max-width: 899px)`, set:
  ```css
  input, textarea, .input, .composer-input {
    font-size: 16px;
  }
  ```
- [ ] **Step 3:** Replace `autoGrow` to:
  - reset height to `auto`
  - maxPx = max(6 * lineHeight, min(0.4 * (visualViewport?.height || window.innerHeight), 8 * lineHeight * 2 or similar — use ~40% vv height with min ~120px max ~45vh)
  - set height to min(scrollHeight, maxPx)
  - set overflowY to scrollHeight > maxPx ? "auto" : "hidden"
- [ ] **Step 4:** Call `autoGrow` from existing viewport resize handler.
- [ ] **Step 5:** Commit: `fix: mobile Safari no-zoom and multi-line composer grow`

---

### Task 4: Rail tabs + file tree + file mode UI

**Files:**
- Modify: `static/index.html`
- Modify: `static/app.css`
- Modify: `static/app.js`

**HTML structure (add):**

Inside `#rail`, after `.rail-header`:
```html
<div class="rail-tabs" role="tablist" aria-label="Sidebar">
  <button type="button" role="tab" id="tab-sessions" aria-selected="true" aria-controls="panel-sessions">Sessions</button>
  <button type="button" role="tab" id="tab-files" aria-selected="false" aria-controls="panel-files">Files</button>
</div>
```

Wrap session search + list + empty in `#panel-sessions`. Add `#panel-files` with file filter, `#file-tree`, `#file-empty`.

In `.main`, add `#file-panel` (hidden by default) with toolbar and `#file-editor` textarea, sibling structure so chat chrome can hide when in file mode.

**JS behaviors:**
- `setRailTab("sessions"|"files")` toggles panels, aria-selected, sessionStorage `grh.railTab`
- When Files tab + selected session with cwd: `loadFsRoot(cwd)` → list `path=""`
- Expand folder: list that rel path, cache, re-render tree
- Click file: `openFile(rel)` → GET read → show file panel, set mainMode file
- Editor input sets dirty if content !== baseline
- Save → PUT write → update baseline
- Insert path → append relative path to composer value + space, mainMode chat, focus input, autoGrow
- Dirty confirm via `window.confirm`
- Session open: if cwd changes, reset fs state

**CSS:**
- `.rail-tabs` segment buttons matching ops aesthetic
- `.file-row` similar to `.session-row` with indent via `--depth` padding
- `.file-panel` flex column full main area; editor flex 1 mono

- [ ] **Step 1:** HTML structure
- [ ] **Step 2:** CSS
- [ ] **Step 3:** JS state + API helpers + render + events
- [ ] **Step 4:** Commit: `feat: Sessions/Files rail tabs with file tree editor`

---

### Task 5: Verification

- [ ] Run full unit suite: `python -m pytest tests/ -q`
- [ ] Manual checklist (if hub runnable): tabs, expand, open, edit save, insert path, search focus no zoom on mobile widths
- [ ] Fix any regressions; commit if needed

---

## Spec coverage checklist

| Spec item | Task |
|---|---|
| Rail Sessions \| Files | 4 |
| Tree root = session cwd | 4 |
| List/read/write API + sandbox | 1–2 |
| File mode edit/save/insert | 4 |
| Dirty confirm | 4 |
| Binary/oversize handling | 1 |
| Mobile 16px + viewport no zoom | 3 |
| Composer auto-grow | 3 |
| No create/rename/delete | (omitted) |
