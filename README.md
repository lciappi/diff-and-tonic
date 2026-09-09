# review.py — local git diff review tool

Single-file, stdlib-only Python. No server dependencies, no auth, no network calls
(the only remote asset is highlight.js from a CDN for syntax colours; the page works
without it, just uncoloured).

## Run

```bash
cd /path/to/your/repo
python3 /path/to/review.py                 # main..<current branch>
python3 /path/to/review.py main feature    # explicit refs
python3 /path/to/review.py main --merge-base   # main...HEAD (from the merge base)
```

Options: `--repo PATH` (default cwd), `--port N` (default 8420), `--no-open`.
It prints the URL and opens your browser.

## Comments whose line moved or disappeared

A diff is not a stable coordinate system: editing the file, committing, or reverting
shifts line numbers around and can drop lines (or whole files) out of the diff. A
comment is never dropped from the UI because of that.

- **Line still there, same text** → renders inline as usual.
- **Line moved** (something was inserted above it) → the stored `snippet` is matched
  against the current diff and the comment renders on the line that actually holds
  that text, tagged `moved from R5`. The old line *number* is deliberately not
  trusted: after an insert it points at unrelated code.
- **Line gone from the diff** → the thread moves to an **orphan block** pinned above
  the diff (`N comments on lines that are no longer in this diff`), tagged `was R322`
  with a tooltip saying whether the line left the diff or now holds different code.
  Reply / Ask agent / Resolve all still work there.
- **File gone from the diff** (reverted, committed elsewhere, outside the range) →
  it stays in the sidebar under **Not in the current diff** with its threads intact,
  and is excluded from the progress and churn numbers so the totals stay honest.

Snippets are backfilled the first time a file is opened, so reviews started before
this existed pick it up automatically.

## Upgrading an existing review

State files written by earlier versions are read as-is and migrated in place on the
next start: each comment gains a stable `id`, `author`, `replies` and `awaiting`, and
everything already recorded (reviewed flags, comment text, notes) is preserved. The
ids are written to disk rather than invented per load, so replies stay attached to
the right comment. Just start the tool again on the same repo.

## State

Everything lives in `<repo>/.review/state.json`, written on every change (atomic
replace). `.review/` is appended to the repo's `.gitignore` on first run.

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
        "text": "why 42?",
        "awaiting": false,
        "resolved": true, "resolved_ts": 1788881420,
        "replies": [{"author": "agent", "text": "only used once here", "ts": 1788881301}]
      }]
    }
  }
}
```

`line` anchors are `R<n>` for the new side and `L<n>` for the old side. Each comment
also stores a `snippet` — the text of the line it was written about — which is what
keeps it attached when the diff changes underneath it (see below).

## UI

- **Overview** (the landing view, `o`, or click the range in the top bar): totals for
  the whole diff — files changed with a status breakdown, additions/deletions/net and
  churn, commits and authors, review progress, comment threads, uncommitted files —
  then churn bars per file (biggest first, click to open) and per file type.
- Sidebar: every changed file with status (A/M/D/R), +/− counts, a **reviewed**
  checkbox, comment count and a 📝 marker when the file has notes.
- Top bar: `X / Y reviewed` progress bar, **Next unreviewed**, **Refresh** (re-runs
  `git diff --name-status` without losing state).
- Main panel: unified diff, per-line syntax highlighting, file-level notes box
  (autosaves 400 ms after you stop typing).
- Click a line number (or double-click the line) to add a comment; multiple comments
  per line are allowed, each editable/deletable inline.
- Every comment is a **thread**: you and the agent reply under it with avatars, and
  the whole thread stays visible inline in the diff for the rest of the review.
- **Resolve** a thread when you're done with it. It stays where it is — dimmed, with a
  green `✓ resolved` chip, collapsed to its first line and a reply count, and the diff
  gutter marker turns from a yellow dot into a green check. Click it to expand, or
  **Reopen** to put it back on the list. Sidebar badges and the header count show
  *open* threads, so resolved ones stop competing for attention without disappearing.
- Keys: `[` / `]` prev/next file, `u` next unreviewed, `r` toggle reviewed,
  `⌘/Ctrl+Enter` save a comment, `Esc` cancel.

## Agent replies

Turn on **Agent replies** in the top bar and every new comment is flagged for the
agent (individual comments can also be flagged with **Ask agent**). Flagged threads
show a pulsing "waiting for the agent" line until answered.

The agent side is a CLI — nothing is sent anywhere, an agent running in your terminal
(Claude Code, a script, you) reads the queue and writes back:

```bash
python3 review.py todo                     # plain text: every open thread, what to address
python3 review.py todo --flagged -c 8      # only threads flagged for the agent, with diff context
python3 review.py pending                  # JSON: flagged comments + diff context
python3 review.py pending --all            # every comment, answered or not
python3 review.py reply app.py 27b8e3d9 "only used once here"
python3 review.py reply --batch -          # JSON list of {path,id,text} on stdin
python3 review.py resolve app.py 27b8e3d9  # mark it dealt with (--reopen undoes)
```

`todo` is the one to hand an agent: plain text, resolved threads hidden, each thread
labelled with its id, anchor, author and marks (`FLAGGED-FOR-YOU`, `line no longer in
the diff`), replies inline, and a footer spelling out the author roles and the two
commands to respond with. `pending` is the same queue as JSON when you want to script
over it.

`pending` skips resolved threads, so resolving one takes it off the agent's queue.
Resolving leaves the `awaiting` flag untouched, so reopening restores the thread
exactly as it was.

`pending` gives each comment's id, the file, which side/line it is anchored to, the
file notes and the surrounding diff lines (`>>>` marks the commented one), so an agent
has enough context to answer without opening anything else. Replies land in
`state.json`; the open page polls `/api/version` every 2s and the thread appears
without a reload (polling pauses while you are typing so nothing is lost).

Three author roles, each with its own look — avatars are generated locally, no avatar
service and no requests:

| `author` | shows as | avatar |
|---|---|---|
| `you` (or absent) | **You** — your git identity is never displayed | identicon, seeded from `user.name`/`user.email` |
| `agent` | **Agent** + `agent` tag | gradient sparkle, purple bubble |
| anything else, e.g. `ai-review-agent` | prettified id (**AI Review Agent**) + `ai` tag | teal magnifier, hue seeded from the id, teal bubble |

So a separate automated reviewer writing as `ai-review-agent` is visually distinct
from both you and the agent you talk to. `review.py reply --author <id>` takes any
id, and `pending` treats a reply from any non-`you` author as answered.

## API (if you want to script against it)

`GET /api/files`, `GET /api/diff?path=…`, `GET /api/state`, `GET /api/version`,
`POST /api/file {path, patch:{reviewed?,notes?,comments?}}`,
`POST /api/reply {path,id,text,author}`, `POST /api/reply/edit {path,id,index,text}`
(empty text deletes), `POST /api/ask {path,id,on}`, `POST /api/resolve {path,id,on}`,
`POST /api/option {agent_replies}`, `POST /api/refresh`.

Comment arrays are replaced wholesale by `/api/file`; replies are keyed by comment
`id` and only change through the reply endpoints, so editing a comment never drops
its thread.
