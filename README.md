```
     ██████╗ ██╗███████╗███████╗     .-------.
     ██╔══██╗██║██╔════╝██╔════╝     |+  ~  -|
     ██║  ██║██║█████╗  █████╗       | ~  + ~|
     ██║  ██║██║██╔══╝  ██╔══╝       |-  ~  +|
     ██████╔╝██║██║     ██║          | +  -  |
     ╚═════╝ ╚═╝╚═╝     ╚═╝          |_______|
             &  T O N I C             '-----'
```

<h1 align="center">diff &amp; tonic</h1>

<p align="center">
Review a big diff in your browser · comment on lines · hand the comments to an agent<br>
<code>one python file</code> · <code>stdlib only</code> · <code>no build step</code> · <code>no accounts</code> · <code>nothing leaves your machine</code>
</p>

```
┌───────────────────────────────────────────────────────────────────────────┐
│ git diff main..feature  ███████░░░░░░  12/39 reviewed  ✦ 3 waiting        │
├─────────────────────┬─────────────────────────────────────────────────────┤
│ ◆ Overview          │ src/monoova/Mapper.java     main → working tree     │
│ ☑ forge.yml      ✓2 │                                                     │
│ ☐ Mapper.java   ● 1 │  41  41   if (code.contains("NOT_FOUND")) {         │
│ ☐ application.yml   │  42   -       return NO_MATCH;                      │
│ ☐ CLAUDE.md      ✦1 │      42 +     return unavailable(code);             │
│                     │     ┌ You ─────────────────────────────────┐        │
│ NOT IN THE DIFF   3 │     │ why not the specific codes?          │        │
│   PhoneNumber.java  │     │ → Agent: fixed, matches PAY_ALIAS_*  │        │
│                     │     └──────────────────────── ✓ resolved ──┘        │
└─────────────────────┴─────────────────────────────────────────────────────┘
```

A left rail of changed files you tick off, the diff in the middle, and every comment a
thread you (or an agent) can reply to and resolve. State is a single JSON file in your
repo, so closing the tab loses nothing.

---

## Pour one

```bash
cd /path/to/your/repo
python3 /path/to/review.py                  # main..<current branch>
python3 /path/to/review.py main feature     # explicit refs
```

It prints a URL and opens your browser. `Ctrl-C` to stop — progress is already saved.

Uncommitted work (staged, unstaged and untracked files) is included automatically when
you're reviewing the checked-out commit, diffed against the base so you see committed
and uncommitted changes together.

## Drink it

- You land on **Overview**: files changed, additions/deletions, commits, progress,
  churn per file and per file type. Click any row to open that file.
- **Sidebar**: every changed file with status, `+`/`−` counts, a **reviewed** checkbox
  and thread badges. Search by filename or folder with **Find a file**. Tick files
  off as you go; the top bar tracks `X / Y reviewed`.
- **Start reviewing / Continue review** jumps to the next unreviewed file. The
  charcoal-and-slate interface adapts to smaller windows and respects reduced motion.
- **Click a line number** (or double-click a line) to comment on it. Add file-level
  thoughts in the **File notes** box — both save as you type.
- **Resolve** a thread when you're done with it. It stays visible, dimmed with a
  `✓ resolved` chip, and stops counting as open.

```
   h / l  prev / next file      j / k  scroll down / up
   i  add comment              o  approve / undo approval
   u  next unreviewed          p  overview
   Shift+j / Shift+k  page down / up
   ⌘/Ctrl+Enter  save comment   Esc  cancel
```

The shortcut bar stays visible at the bottom, even while scrolling a long diff.
The main shortcuts sit together under your right hand: **H J K L** and **U I O P**.
**O** approves the current file (marks it reviewed); press it again to undo approval.
**I** opens a comment on the code line you selected with a click, or the first visible
line if none is selected in view. Files without code lines use File notes instead.
Shortcuts stay inactive while typing in a text field. The bar's actions are clickable too.
**J/K** scroll the review panel without needing to click it first. Hold either key to
keep scrolling, or add **Shift** to move a page at a time. **[ / ]** still switch files;
**R/C** remain aliases for approve/comment. Arrow keys, Page Up/Down, and Home/End
also work, and text fields retain normal typing and cursor keys.

Everything is stored in `<repo>/.review/state.json`, written immediately on every
change. Restart the server or reload the page and your progress is there. `.review/`
is added to the repo's `.gitignore` on first run.

## Share the round with an agent

Tick **Agent replies** in the top bar and every new comment is flagged for an agent
(or flag single threads with **Ask agent**). Then, in a terminal or an agent session:

```bash
python3 review.py todo                      # every open thread, plain text
python3 review.py reply <file> <id> "..."   # answer one
python3 review.py resolve <file> <id>       # mark it done
```

Replies show up in the open page within a couple of seconds — no reload.
**See [AGENT.md](AGENT.md) for the instructions to give an agent**, including a prompt
you can paste.

---

# Reference

## Options

```
--repo PATH      repo to review (default: cwd)
--port N         port to bind (default: 8420)
--merge-base     diff base...head, i.e. from the merge base
--worktree / --no-worktree
                 force include/exclude uncommitted changes (default: auto —
                 included when head is the checked-out commit)
--no-untracked   skip untracked files (still shows staged/unstaged)
--no-open        don't open a browser
```

Note that `git diff main..main` is an empty range — a commit against itself. If the
committed side should contribute, give a base that differs from `HEAD`
(`review.py origin/main`, or `review.py main --merge-base` on a feature branch).

## Threads and authors

Every comment is a thread: replies nest under it with avatars and stay visible inline
in the diff for the rest of the review. Three author roles, each with its own look —
avatars are generated locally, no avatar service and no requests:

| `author` | shows as | avatar |
|---|---|---|
| `you` (or absent) | **You** — your git identity is never displayed | identicon, seeded from `user.name`/`user.email` |
| `agent` | **Agent** + `agent` tag | gradient sparkle, slate-blue bubble |
| anything else, e.g. `ai-review-agent` | prettified id (**AI Review Agent**) + `ai` tag | teal magnifier, hue seeded from the id, teal bubble |

So a separate automated reviewer writing as `ai-review-agent` is visually distinct
from both you and the agent you talk to.

Resolved threads dim, collapse to their first line plus a reply count, and turn the
diff gutter marker from a yellow dot into a green check. Click to expand, **Reopen**
to put it back on the list. Sidebar badges and header counts show *open* threads.

## Comments whose line moved or disappeared

A diff is not a stable coordinate system: editing, committing or reverting shifts line
numbers and can drop lines (or whole files) out of the diff. No comment is ever hidden
because of that.

- **Line still there, same text** → renders inline as usual.
- **Line moved** (something inserted above it) → the stored `snippet` is matched
  against the current diff and the comment renders on the line that actually holds
  that text, tagged `moved from R5`. The old line *number* is deliberately not
  trusted: after an insert it points at unrelated code.
- **Line gone from the diff** → the thread moves to an **orphan block** pinned above
  the diff (`N comments on lines that are no longer in this diff`), tagged `was R322`,
  with reply / ask / resolve still working.
- **File gone from the diff** (reverted, committed elsewhere, outside the range) → it
  stays in the sidebar under **Not in the current diff** with its threads intact, and
  is excluded from progress and churn so the totals stay honest.

Snippets are backfilled the first time a file is opened, so older reviews pick this up
automatically.

## State file

```json
{
  "base": "main", "head": "feature", "range": "main..feature",
  "worktree": true, "agent_replies": true,
  "files": {
    "src/app.py": {
      "reviewed": true,
      "notes": "return value changed",
      "comments": [{
        "id": "27b8e3d9", "line": "R2", "author": "you", "ts": 1788881226,
        "text": "why 42?", "snippet": "    return 42",
        "awaiting": false,
        "resolved": true, "resolved_ts": 1788881420,
        "replies": [{"author": "agent", "text": "only used once here", "ts": 1788881301}]
      }]
    }
  }
}
```

`line` anchors are `R<n>` for the new side, `L<n>` for the old side. `snippet` is the
text of the anchored line, used to re-find it when the diff changes. `awaiting` means
"flagged for an agent reply".

State written by an earlier version is migrated in place on the next start — comments
gain a stable `id`, `author`, `replies`, `awaiting` and `resolved`, and everything
already recorded is preserved. Ids are written to disk rather than invented per load,
so replies stay attached to the right comment.

## HTTP API

`GET /api/files`, `GET /api/diff?path=…`, `GET /api/state`, `GET /api/version`,
`POST /api/file {path, patch:{reviewed?,notes?,comments?}}`,
`POST /api/reply {path,id,text,author}`, `POST /api/reply/edit {path,id,index,text}`
(empty text deletes), `POST /api/ask {path,id,on}`, `POST /api/resolve {path,id,on}`,
`POST /api/option {agent_replies}`, `POST /api/refresh`.

Comment arrays are replaced wholesale by `/api/file`; replies are keyed by comment
`id` and only change through the reply endpoints, so editing a comment never drops its
thread. The page polls `/api/version` every 2s and repaints when the file changes on
disk, which is how CLI replies appear live (polling pauses while you're typing).

---

<p align="center"><sub>the command is still <code>review.py</code> — the tonic is optional</sub></p>
