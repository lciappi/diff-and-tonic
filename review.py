#!/usr/bin/env python3
"""
review.py - a local, self-contained git diff review tool.

Usage:
    python3 review.py [base] [head] [options]

    base   base ref to diff from (default: main)
    head   head ref to diff to   (default: current branch)

Uncommitted work (staged, unstaged and untracked files) is folded into the same
review list whenever head is the checked-out commit; those files are diffed
against the base so you see committed + uncommitted changes together.

Options:
    --repo PATH      repo to review (default: cwd)
    --port N         port to bind (default: 8420)
    --merge-base     diff base...head (from the merge base) instead of base..head
    --worktree/--no-worktree
                     force include/exclude uncommitted changes (default: auto -
                     included when head is the checked-out commit)
    --no-untracked   skip untracked files (still shows staged/unstaged)
    --no-open        don't open a browser

State lives in <repo>/.review/state.json. Nothing leaves your machine.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import textwrap
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------------------------------------------------------- git plumbing

class GitError(RuntimeError):
    pass


def git(repo, *args, **kw):
    ok = kw.pop("ok", (0,))
    assert not kw, kw
    p = subprocess.run(
        ["git", "-C", repo] + list(args),
        capture_output=True, text=True, errors="replace",
    )
    if p.returncode not in ok:
        raise GitError((p.stderr or p.stdout).strip() or "git %s failed" % (args,))
    return p.stdout


def repo_root(path):
    return git(path, "rev-parse", "--show-toplevel").strip()


def current_branch(repo):
    name = git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
    return "HEAD" if name == "HEAD" else name


def range_stats(repo, rng):
    """Commit-level facts about the range (empty range -> zeros)."""
    out = {"commits": 0, "authors": []}
    try:
        out["commits"] = int(git(repo, "rev-list", "--count", rng).strip() or 0)
        for line in git(repo, "shortlog", "-sn", "--no-merges", rng).splitlines():
            n, _, who = line.strip().partition("\t")
            if who:
                out["authors"].append({"name": who, "commits": int(n)})
    except (GitError, ValueError):
        pass
    return out


def git_user(repo):
    def cfg(k):
        try:
            return git(repo, "config", "--get", k).strip()
        except GitError:
            return ""
    name = cfg("user.name") or os.environ.get("USER") or "you"
    return {"name": name, "email": cfg("user.email"), "seed": name + cfg("user.email")}


def verify_ref(repo, ref):
    try:
        git(repo, "rev-parse", "--verify", "--quiet", ref + "^{commit}")
    except GitError:
        raise SystemExit("error: unknown ref %r in %s" % (ref, repo))


def split_z(out):
    parts = out.split("\0")
    if parts and parts[-1] == "":
        parts.pop()
    return parts


def name_status(repo, spec):
    """{path: {"path","old","status"}} for `git diff --name-status <spec>`."""
    out = {}
    parts = split_z(git(repo, "diff", "--name-status", "-z", spec))
    i = 0
    while i < len(parts):
        status = parts[i]
        if not status:
            i += 1
            continue
        if status[0] in ("R", "C") and i + 2 < len(parts):
            path, old, i = parts[i + 2], parts[i + 1], i + 3
        else:
            if i + 1 >= len(parts):
                break
            path, old, i = parts[i + 1], None, i + 2
        out[path] = {"path": path, "old": old, "status": status}
    return out


def numstat(repo, spec):
    """{path: (adds, dels)}; None means binary."""
    out = {}
    parts = split_z(git(repo, "diff", "--numstat", "-z", spec))
    i = 0
    while i < len(parts):
        bits = parts[i].split("\t")
        if len(bits) < 3:
            i += 1
            continue
        adds, dels, path = bits[0], bits[1], bits[2]
        if path == "" and i + 2 < len(parts):   # rename: adds\tdels\t\0old\0new
            path, i = parts[i + 2], i + 3
        else:
            i += 1
        out[path] = (None if adds == "-" else int(adds),
                     None if dels == "-" else int(dels))
    return out


def worktree_status(repo):
    """({path: {"staged","unstaged","old"}}, [untracked paths]) from git status."""
    dirty, untracked = {}, []
    parts = split_z(git(repo, "status", "--porcelain", "-z", "-uall"))
    i = 0
    while i < len(parts):
        rec = parts[i]
        i += 1
        if len(rec) < 4:
            continue
        x, y, path = rec[0], rec[1], rec[3:]
        old = None
        if x in ("R", "C") and i < len(parts):
            old = parts[i]
            i += 1
        if x == "?" or x == "!":
            if x == "?":
                untracked.append(path)
            continue
        dirty[path] = {"staged": x not in (" ", "?"),
                       "unstaged": y not in (" ", "?"),
                       "old": old}
    return dirty, untracked


TEXT_CAP = 2 * 1024 * 1024   # untracked files larger than this aren't rendered


def untracked_stat(repo, path):
    """(adds, note) for an untracked file - adds=None when binary/too large."""
    full = os.path.join(repo, path)
    try:
        size = os.path.getsize(full)
        if size > TEXT_CAP:
            return None, "untracked file too large to display (%d bytes)" % size
        with open(full, "rb") as fh:
            blob = fh.read()
    except OSError as e:
        return None, "cannot read untracked file (%s)" % e
    if b"\0" in blob[:8000]:
        return None, "untracked binary file (%d bytes)" % len(blob)
    n = blob.count(b"\n")
    if blob and not blob.endswith(b"\n"):
        n += 1
    return n, None


def stateful_paths(store):
    """Paths carrying comments or notes - they must stay reachable."""
    return [p for p, f in store.data.get("files", {}).items()
            if f.get("comments") or (f.get("notes") or "").strip()]


def collect_files(repo, base, rng, worktree=True, untracked=True, keep=()):
    """Files to review: the committed range plus uncommitted work.

    Each entry carries `dspec` (the git diff argument used to render it) and
    `label` (what that comparison is, for the UI).
    """
    entries, index = [], {}

    def add(e):
        entries.append(e)
        index[e["path"]] = e
        return e

    ns, nums = name_status(repo, rng), numstat(repo, rng)
    for path, e in ns.items():
        a, d = nums.get(path, (0, 0))
        add(dict(e, adds=a, dels=d, dirty=None, dspec=rng, label=rng))

    if not worktree:
        return entries

    dset, uset = worktree_status(repo)
    if untracked is False:
        uset = []

    if dset:
        # base -> working tree, so a dirty file shows committed + uncommitted
        wns, wnums = name_status(repo, base), numstat(repo, base)
        fallback = [p for p in dset if p not in wns]
        hns = hnums = {}
        if fallback:                      # unchanged vs base, but dirty vs HEAD
            hns, hnums = name_status(repo, "HEAD"), numstat(repo, "HEAD")
        for path, flags in dset.items():
            src = wns.get(path)
            spec, label, stats = base, "%s \u2192 working tree" % base, wnums
            if src is None:
                src, spec, stats = hns.get(path), "HEAD", hnums
                label = "HEAD \u2192 working tree"
            tag = ("staged+unstaged" if flags["staged"] and flags["unstaged"]
                   else "staged" if flags["staged"] else "unstaged")
            a, d = stats.get(path, (0, 0))
            e = index.get(path)
            if e is None:
                e = add({"path": path, "old": None, "status": "M"})
            e.update(status=(src or {}).get("status", e.get("status", "M")),
                     old=(src or {}).get("old") or flags["old"],
                     adds=a, dels=d, dirty=tag, dspec=spec, label=label)

    for path in uset:
        if path in index:
            continue
        adds, note = untracked_stat(repo, path)
        add({"path": path, "old": None, "status": "A", "adds": adds, "dels": 0,
             "dirty": "untracked", "dspec": None, "label": "untracked file",
             "note": note})

    # a reverted/committed file drops out of the diff, but its threads must not
    for path in keep:
        if path in index:
            continue
        add({"path": path, "old": None, "status": "-", "adds": 0, "dels": 0,
             "dirty": None, "stale": True, "dspec": None,
             "label": "not in the current diff"})

    return entries


HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")
SKIP_PREFIXES = (
    "diff --git", "index ", "--- ", "+++ ", "new file mode", "deleted file mode",
    "old mode", "new mode", "similarity index", "dissimilarity index",
    "rename from", "rename to", "copy from", "copy to",
)


def file_diff(repo, entry):
    """Parse `git diff` for one entry into display rows.

    Each code row carries a stable key: R<n> for the new side, L<n> for the old
    side. Comments anchor to that key, so they survive re-reading the diff.
    """
    if entry.get("stale"):
        return [{"t": "meta", "text": "This file is no longer part of the diff "
                 "(reverted, committed elsewhere, or outside the range). Its "
                 "comments are kept below."}]
    if entry.get("dirty") == "untracked":
        if entry.get("note"):
            return [{"t": "meta", "text": entry["note"]}]
        text = git(repo, "diff", "--no-index", "--", os.devnull, entry["path"],
                   ok=(0, 1))
    else:
        args = ["diff", entry["dspec"], "--", entry["path"]]
        if entry.get("old"):
            args.append(entry["old"])   # renames: ask for both sides
        text = git(repo, *args)
    rows = []
    oldno = newno = 0
    for line in text.split("\n"):
        if line == "":
            continue
        if line.startswith("@@"):
            m = HUNK_RE.match(line)
            if m:
                oldno, newno = int(m.group(1)), int(m.group(3))
            rows.append({"t": "hunk", "text": line})
            continue
        if line.startswith("\\"):                     # \ No newline at end of file
            rows.append({"t": "meta", "text": line})
            continue
        if line.startswith("Binary files") or line.startswith("GIT binary patch"):
            rows.append({"t": "meta", "text": line})
            continue
        if line.startswith(SKIP_PREFIXES):
            continue
        head, body = line[0], line[1:]
        if head == "+":
            rows.append({"t": "add", "new": newno, "text": body, "k": "R%d" % newno})
            newno += 1
        elif head == "-":
            rows.append({"t": "del", "old": oldno, "text": body, "k": "L%d" % oldno})
            oldno += 1
        else:
            rows.append({"t": "ctx", "old": oldno, "new": newno, "text": body,
                         "k": "R%d" % newno})
            oldno += 1
            newno += 1
    if not rows:
        rows.append({"t": "meta", "text": "(no textual difference for %s)" % entry["label"]})
    return rows


# --------------------------------------------------------------------- state

class Store:
    """.review/state.json, reloaded on disk change so the CLI and the running
    server can both write it without clobbering each other."""

    def __init__(self, root):
        self.dir = os.path.join(root, ".review")
        self.path = os.path.join(self.dir, "state.json")
        self.lock = threading.Lock()
        self.mtime = None
        os.makedirs(self.dir, exist_ok=True)
        self.data = self._load()

    # -- disk
    def _stamp(self):
        try:
            return os.stat(self.path).st_mtime_ns
        except OSError:
            return None

    def _load(self):
        try:
            with open(self.path) as fh:
                data = json.load(fh)
        except (IOError, ValueError):
            data = {}
        data.setdefault("files", {})
        self.data = data
        self.mtime = self._stamp()
        if self._migrate():
            # ids must be on disk, not invented per load, or replies would
            # re-key themselves every time the file is re-read
            self._flush()
        return data

    def _migrate(self):
        """Upgrade a state.json written by an older version, in place."""
        changed = False
        for f in self.data["files"].values():
            for key, default in (("reviewed", False), ("comments", []), ("notes", "")):
                if key not in f:
                    f[key] = default
                    changed = True
            for c in f["comments"]:
                for key, make in (("id", new_id), ("author", lambda: "you"),
                                  ("replies", list), ("awaiting", lambda: False),
                                  ("resolved", lambda: False),
                                  ("ts", lambda: int(time.time()))):
                    if key not in c:
                        c[key] = make()
                        changed = True
        return changed

    def sync(self):
        """Pick up writes made by another process (e.g. `review.py reply`)."""
        if self._stamp() != self.mtime:
            self.data = self._load()
        return self.data

    def _flush(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(self.data, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, self.path)
        self.mtime = self._stamp()

    def version(self):
        return self._stamp() or 0

    # -- accessors
    def file(self, path):
        f = self.data["files"].get(path)
        if not f:
            f = {"reviewed": False, "comments": [], "notes": ""}
            self.data["files"][path] = f
        f.setdefault("reviewed", False)
        f.setdefault("comments", [])
        f.setdefault("notes", "")
        for c in f["comments"]:
            c.setdefault("id", new_id())
            c.setdefault("author", "you")
            c.setdefault("replies", [])
            c.setdefault("awaiting", False)
            c.setdefault("resolved", False)
        return f

    def comment(self, path, cid):
        for c in self.file(path)["comments"]:
            if c["id"] == cid:
                return c
        raise KeyError("no comment %s on %s" % (cid, path))

    # -- mutations
    def patch(self, path, patch):
        """Whole-array comment updates from the UI; replies survive by id."""
        with self.lock:
            self.sync()
            f = self.file(path)
            if "reviewed" in patch:
                f["reviewed"] = bool(patch["reviewed"])
            if "notes" in patch:
                f["notes"] = str(patch["notes"])
            if "comments" in patch:
                prev = {c["id"]: c for c in f["comments"]}
                out = []
                for c in patch["comments"]:
                    text = str(c.get("text", ""))
                    if not text.strip():
                        continue
                    old = prev.get(c.get("id")) or {}
                    out.append({
                        "id": c.get("id") or old.get("id") or new_id(),
                        "line": str(c.get("line", old.get("line", ""))),
                        "text": text,
                        "snippet": c.get("snippet", old.get("snippet", "")),
                        "author": c.get("author") or old.get("author") or "you",
                        "ts": c.get("ts") or old.get("ts") or int(time.time()),
                        # replies only change through add_reply()
                        "replies": old.get("replies", []),
                        "awaiting": bool(c.get("awaiting", old.get("awaiting", False))),
                        "resolved": bool(c.get("resolved", old.get("resolved", False))),
                    })
                f["comments"] = out
            self._flush()
            return f

    def add_reply(self, path, cid, text, author="agent"):
        with self.lock:
            self.sync()
            c = self.comment(path, cid)
            c["replies"].append({"author": author, "text": str(text),
                                 "ts": int(time.time())})
            if author == "agent":
                c["awaiting"] = False
            self._flush()
            return c

    def edit_reply(self, path, cid, idx, text):
        with self.lock:
            self.sync()
            c = self.comment(path, cid)
            if not 0 <= idx < len(c["replies"]):
                raise KeyError("no reply %d" % idx)
            if str(text).strip():
                c["replies"][idx]["text"] = str(text)
            else:
                c["replies"].pop(idx)
            self._flush()
            return c

    def set_resolved(self, path, cid, on):
        with self.lock:
            self.sync()
            c = self.comment(path, cid)
            c["resolved"] = bool(on)
            if c["resolved"]:
                c["resolved_ts"] = int(time.time())
            else:
                c.pop("resolved_ts", None)
            # `awaiting` is left alone: `pending` filters resolved threads out,
            # so reopening restores whatever state the thread was in
            self._flush()
            return c

    def set_awaiting(self, path, cid, on):
        with self.lock:
            self.sync()
            c = self.comment(path, cid)
            c["awaiting"] = bool(on)
            self._flush()
            return c

    def meta(self, **kw):
        with self.lock:
            self.sync()
            self.data.update(kw)
            self._flush()


def new_id():
    return os.urandom(4).hex()


def ensure_gitignore(root):
    gi = os.path.join(root, ".gitignore")
    want = ".review/"
    try:
        with open(gi) as fh:
            lines = [l.strip() for l in fh]
    except IOError:
        lines = []
    if want in lines or ".review" in lines:
        return False
    with open(gi, "a") as fh:
        if lines and lines[-1] != "":
            fh.write("\n")
        fh.write("# local code review state (review.py)\n%s\n" % want)
    return True

# ---------------------------------------------------------------------- server

class Handler(BaseHTTPRequestHandler):
    ctx = None   # set in main()

    def log_message(self, *a):
        pass

    # -- helpers
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj))

    def _err(self, msg, code=400):
        self._json({"error": str(msg)}, code)

    def _backfill_snippets(self, path, rows):
        """Remember each comment's line text, once, so it can be re-found."""
        store = self.ctx["store"]
        by_key = {r["k"]: r for r in rows if r.get("k")}
        dirty = False
        for c in store.file(path)["comments"]:
            if not c.get("snippet") and c["line"] in by_key:
                c["snippet"] = by_key[c["line"]].get("text", "")
                dirty = bool(c["snippet"]) or dirty
        if dirty:
            with store.lock:
                store._flush()

    def _files_payload(self):
        c = self.ctx
        c["store"].sync()
        out = []
        for e in c["files"]:
            st = c["store"].file(e["path"])
            pub = dict(e, state={"reviewed": st["reviewed"],
                                 "comments": st["comments"],
                                 "notes": st["notes"]})
            pub.pop("dspec", None)          # internal
            out.append(pub)
        return {"meta": {"base": c["base"], "head": c["head"], "range": c["range"],
                         "worktree": c["worktree"], "repo": c["root"],
                         "state_file": c["store"].path, "user": c["user"],
                         "stats": c["stats"],
                         "agent_replies": bool(c["store"].data.get("agent_replies")),
                         "version": c["store"].version()},
                "files": out}

    # -- routes
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        try:
            if u.path == "/":
                return self._send(200, PAGE, "text/html; charset=utf-8")
            if u.path == "/api/files":
                return self._json(self._files_payload())
            if u.path == "/api/version":
                return self._json({"version": self.ctx["store"].version()})
            if u.path == "/api/state":
                return self._json(self.ctx["store"].data)
            if u.path == "/api/diff":
                path = (q.get("path") or [""])[0]
                entry = next((e for e in self.ctx["files"] if e["path"] == path), None)
                if not entry:
                    return self._err("unknown file: %s" % path, 404)
                rows = file_diff(self.ctx["root"], entry)
                self._backfill_snippets(path, rows)
                return self._json({"path": path, "rows": rows})
            return self._err("not found", 404)
        except GitError as e:
            return self._err(e, 500)

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(n) or "{}")
        except ValueError as e:
            return self._err("bad json: %s" % e)
        try:
            if u.path == "/api/file":
                path = payload.get("path")
                if not path:
                    return self._err("path required")
                st = self.ctx["store"].patch(path, payload.get("patch") or {})
                return self._json({"path": path, "state": st})
            if u.path == "/api/reply":
                st = self.ctx["store"].add_reply(
                    payload["path"], payload["id"], payload.get("text", ""),
                    payload.get("author", "agent"))
                return self._json({"comment": st})
            if u.path == "/api/reply/edit":
                st = self.ctx["store"].edit_reply(
                    payload["path"], payload["id"], int(payload["index"]),
                    payload.get("text", ""))
                return self._json({"comment": st})
            if u.path == "/api/ask":
                st = self.ctx["store"].set_awaiting(
                    payload["path"], payload["id"], payload.get("on", True))
                return self._json({"comment": st})
            if u.path == "/api/resolve":
                st = self.ctx["store"].set_resolved(
                    payload["path"], payload["id"], payload.get("on", True))
                return self._json({"comment": st})
            if u.path == "/api/option":
                self.ctx["store"].meta(
                    agent_replies=bool(payload.get("agent_replies")))
                return self._json({"agent_replies":
                                   bool(self.ctx["store"].data.get("agent_replies"))})
            if u.path == "/api/refresh":
                c = self.ctx
                c["files"] = collect_files(c["root"], c["base"], c["range"],
                                           worktree=c["worktree"],
                                           untracked=c["untracked"],
                                           keep=stateful_paths(c["store"]))
                return self._json(self._files_payload())
            return self._err("not found", 404)
        except KeyError as e:
            return self._err(e, 404)
        except GitError as e:
            return self._err(e, 500)

# ------------------------------------------------------------------ the page

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>diff &amp; tonic</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/github-dark.min.css">
<script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/highlight.min.js"></script>
<style>
  :root{
    color-scheme:dark;
    --bg:#181a1d; --bg2:#202327; --bg3:#2b2f34; --line:#3c4148;
    --fg:#edf0f3; --dim:#a5adb7; --accent:#a9bfd8;
    --add:#202e44; --addln:#91bcf4; --del:#392324; --delln:#f09d95;
    /* Blue additions and coral deletions remain distinct without relying on hue alone. */
    --c-add:#82aee8; --c-del:#dc9186;
    --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace;
  }
  *{box-sizing:border-box}
  html,body{height:100%;margin:0}
  body{background:var(--bg);color:var(--fg);font:13px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;display:flex;flex-direction:column}
  a{color:var(--accent)}
  button{font:inherit;color:var(--fg);background:var(--bg3);border:1px solid var(--line);border-radius:6px;padding:3px 9px;cursor:pointer}
  button:hover{border-color:var(--dim)}
  header{display:flex;align-items:center;gap:14px;padding:9px 14px;border-bottom:1px solid var(--line);background:var(--bg2);flex:0 0 auto}
  header .rng{font-family:var(--mono);font-size:12px;color:var(--dim);white-space:nowrap}
  header .rng b{color:var(--fg)}
  .bar{flex:1;min-width:120px;height:8px;border-radius:99px;background:var(--bg3);overflow:hidden}
  .bar>i{display:block;height:100%;background:var(--addln);width:0;transition:width .18s}
  #pct{font-variant-numeric:tabular-nums;white-space:nowrap}
  #wrap{flex:1;display:flex;min-height:0}
  aside{width:340px;flex:0 0 auto;border-right:1px solid var(--line);overflow:auto;background:var(--bg2)}
  aside .head{padding:8px 12px;font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--dim);position:sticky;top:0;background:var(--bg2);border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:center}
  .f{display:flex;align-items:flex-start;gap:8px;padding:6px 10px;border-bottom:1px solid #21262d;cursor:pointer}
  .f:hover{background:var(--bg3)}
  .f.sel{background:#1f2b45;box-shadow:inset 3px 0 0 var(--accent)}
  .f.done .nm{color:var(--dim);text-decoration:line-through}
  .f input{margin:3px 0 0;accent-color:var(--addln);cursor:pointer}
  .f .body{min-width:0;flex:1}
  .f .nm{font-family:var(--mono);font-size:12px;word-break:break-all}
  .f .sub{font-size:11px;color:var(--dim);display:flex;gap:8px;margin-top:2px;flex-wrap:wrap}
  .st{font-family:var(--mono);font-weight:600}
  .st.A{color:var(--addln)} .st.M{color:#d29922} .st.D{color:var(--delln)} .st.R{color:var(--accent)}
  .plus{color:var(--addln)} .minus{color:var(--delln)}
  .cbadge{background:#3d2f04;color:#e3b341;border-radius:99px;padding:0 6px;font-weight:600}
  main{flex:1;overflow:auto;min-width:0}
  .empty{padding:40px;color:var(--dim);text-align:center}
  .fhead{position:sticky;top:0;z-index:2;background:var(--bg);border-bottom:1px solid var(--line);padding:10px 14px}
  .fhead h1{margin:0 0 6px;font:600 14px/1.4 var(--mono);word-break:break-all}
  .fhead .row{display:flex;align-items:center;gap:12px;flex-wrap:wrap;color:var(--dim);font-size:12px}
  .fhead label{display:flex;align-items:center;gap:5px;cursor:pointer;color:var(--fg)}
  .fhead input[type=checkbox]{accent-color:var(--addln);cursor:pointer}
  textarea{width:100%;background:var(--bg2);color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:7px 9px;font:12px/1.5 var(--mono);resize:vertical}
  textarea:focus{outline:none;border-color:var(--accent)}
  .notes{padding:10px 14px;border-bottom:1px solid var(--line)}
  .notes .lbl{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--dim);margin-bottom:5px;display:flex;gap:8px}
  .saved{color:var(--addln);opacity:0;transition:opacity .2s}
  .saved.on{opacity:1}
  table.diff{border-collapse:collapse;width:100%;font:12px/1.55 var(--mono);table-layout:fixed}
  table.diff td{padding:0 6px;vertical-align:top}
  col.c-ln{width:52px}
  td.ln{width:52px;text-align:right;color:#6e7681;user-select:none;background:var(--bg2);border-right:1px solid var(--line);font-size:11px}
  td.code{white-space:pre-wrap;word-break:break-word;cursor:text}
  tr.add td.code{background:var(--add)} tr.add td.ln{background:#122b1c}
  tr.del td.code{background:var(--del)} tr.del td.ln{background:#33161a}
  tr.hunk td{background:var(--bg3);color:#7d8590;padding:3px 6px;border-top:1px solid var(--line);border-bottom:1px solid var(--line)}
  tr.meta td{color:var(--dim);font-style:italic}
  tr.code-row:hover td.ln{background:var(--accent);color:#0d1117;cursor:pointer}
  tr.has td.ln::after{content:"\25CF";color:#e3b341;float:left;margin-left:2px}
  td.code .sig{color:var(--dim)}
  tr.cmt td{padding:6px 6px 8px 58px;background:#161b22;border-left:2px solid #e3b341}
  .thr{margin:4px 0 2px}
  .msg{display:flex;gap:8px;align-items:flex-start;margin:4px 0}
  .msg.reply{margin-left:26px}
  .av{flex:0 0 auto;border-radius:7px;box-shadow:0 0 0 1px rgba(255,255,255,.12),0 1px 4px rgba(0,0,0,.45)}
  .av.agent{box-shadow:0 0 0 1px rgba(145,172,198,.55),0 0 10px rgba(107,142,175,.3)}
  .av.bot{box-shadow:0 0 0 1px rgba(57,197,187,.5),0 0 10px rgba(31,150,180,.4)}
  .bub{flex:1;min-width:0;background:var(--bg3);border:1px solid var(--line);border-radius:8px;padding:6px 9px;font:12px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
  .msg.reply .bub{background:#191f2e;border-color:#2b3450}
  .msg.agent .bub{background:linear-gradient(180deg,#26313e,#202730);border-color:#435366}
  .msg.bot .bub{background:linear-gradient(180deg,#12262b,#101d22);border-color:#26454e}
  .who{display:flex;align-items:center;gap:7px;margin-bottom:2px;color:var(--dim);font-size:11px}
  .who b{color:var(--fg);font-weight:600}
  .who .tag{background:#2b3644;color:#adc5df;border-radius:99px;padding:0 6px;font-size:10px;text-transform:uppercase;letter-spacing:.05em}
  .who .tag.bot{background:#102c33;color:#39c5bb}
  .who .tools{margin-left:auto;display:flex;gap:4px;opacity:0;transition:opacity .12s}
  .msg:hover .who .tools{opacity:1}
  .who .tools button{padding:0 5px;font-size:11px;background:transparent;color:var(--dim)}
  .who .tools button:hover{color:var(--fg)}
  .txt{white-space:pre-wrap;word-break:break-word}
  .acts{display:flex;gap:6px;align-items:center;margin:4px 0 0 30px}
  .acts button{font-size:11px;padding:2px 8px}
  .thr.done{opacity:.5;transition:opacity .12s}
  .thr.done:hover{opacity:.8}
  .thr.done .bub{cursor:pointer}
  .thr.done .txt.clamp{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .ok{display:inline-flex;align-items:center;gap:4px;background:#202e44;color:var(--addln);border-radius:99px;padding:0 7px;font-size:10px;text-transform:uppercase;letter-spacing:.05em;font-weight:600}
  .more{color:var(--dim);font-size:11px;margin-left:6px}
  tr.has.settled td.ln::after{color:var(--addln);content:"\2713"}
  .cbadge.settled{background:#202e44;color:var(--addln)}
  .orph{margin:0;padding:10px 14px 12px;border-bottom:1px solid var(--line);background:#1a1712}
  .orph .oh{display:flex;align-items:center;gap:8px;font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:#e3b341;margin-bottom:4px}
  .moved{background:#3d2f04;color:#e3b341;border-radius:99px;padding:0 6px;font-size:10px;text-transform:uppercase;letter-spacing:.05em;font-weight:600}
  .f.stale .nm{color:var(--dim)}
  .await{display:inline-flex;align-items:center;gap:5px;color:#adc5df;font-size:11px}
  .await::before{content:"";width:6px;height:6px;border-radius:99px;background:#adc5df;animation:pulse 1.4s infinite}
  @keyframes pulse{0%,100%{opacity:.25;transform:scale(.8)}50%{opacity:1;transform:scale(1.15)}}
  .cmt-ed{margin:4px 0 4px 30px}
  .cmt-ed .btns{display:flex;gap:6px;margin-top:5px;align-items:center}
  .cmt-ed .btns .why{color:var(--dim);font-size:11px}
  .me{display:flex;align-items:center;gap:6px;white-space:nowrap;font-size:12px}
  .me .nm{color:var(--dim);max-width:130px;overflow:hidden;text-overflow:ellipsis}
  .opt{display:flex;align-items:center;gap:5px;white-space:nowrap;cursor:pointer;font-size:12px;color:var(--dim)}
  .opt input{accent-color:#6b8eaf;cursor:pointer}
  .opt.on{color:#adc5df}
  .abadge{background:#2b3644;color:#adc5df;border-radius:99px;padding:0 6px;font-weight:600}
  .dot{width:7px;height:7px;border-radius:99px;background:#adc5df;display:inline-block;animation:pulse 1.4s infinite}
  /* ---- overview ---- */
  .ovw{padding:14px 16px 30px}
  .tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(148px,1fr));gap:10px}
  .tile{background:var(--bg2);border:1px solid var(--line);border-radius:10px;padding:9px 12px}
  .tile .k{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--dim)}
  .tile .v{font:600 23px/1.2 var(--mono);margin-top:3px;font-variant-numeric:tabular-nums}
  .tile .s{font-size:11px;color:var(--dim);margin-top:2px;min-height:1em}
  .sw{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:5px}
  .sw.d{background-image:repeating-linear-gradient(45deg,rgba(0,0,0,.4) 0 2px,transparent 2px 4px)}
  .sec{margin-top:20px}
  .sec .h{display:flex;align-items:center;gap:10px;font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--dim);margin-bottom:6px}
  .sec .h button{font-size:11px;padding:1px 8px;text-transform:none;letter-spacing:0}
  .legend{display:flex;gap:12px;margin-left:auto;text-transform:none;letter-spacing:0}
  .legend span{display:flex;align-items:center}
  .meter{height:10px;border-radius:5px;background:var(--bg3);overflow:hidden}
  .meter>i{display:block;height:100%;background:var(--c-add)}
  .rows{display:grid;gap:3px}
  .brow{display:grid;grid-template-columns:minmax(120px,2fr) minmax(90px,3fr) 110px;gap:12px;align-items:center;padding:3px 6px;border-radius:6px;cursor:pointer}
  .brow:hover{background:var(--bg2)}
  .brow .p{font:12px/1.5 var(--mono);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .brow .p .tick{color:var(--addln)}
  .bar2{display:flex;gap:2px;height:9px;border-radius:4px;background:var(--bg3);overflow:hidden}
  .bar2 i{display:block;height:100%;min-width:0}
  .bar2 .a{background:var(--c-add)}
  .bar2 .d{background:var(--c-del);background-image:repeating-linear-gradient(45deg,rgba(0,0,0,.4) 0 2px,transparent 2px 4px)}
  .nums{font:11px/1.5 var(--mono);color:var(--dim);text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
  .chips{display:flex;gap:6px;flex-wrap:wrap}
  .chip{background:var(--bg2);border:1px solid var(--line);border-radius:99px;padding:1px 9px;font-size:11px;color:var(--dim)}
  .f.ov .nm{color:var(--accent)}
  #rng{cursor:pointer}
  .hint{padding:8px 14px;color:#6e7681;font-size:11px;border-top:1px solid var(--line)}
  kbd{font-family:var(--mono);background:var(--bg3);border:1px solid var(--line);border-radius:4px;padding:0 4px}
  /* A little tonic: quiet surfaces, slate accents, room to breathe. */
  body{font-size:13px}
  button{padding:7px 12px;border-radius:8px;transition:background .15s,border-color .15s,transform .15s}
  button:hover{background:#343a41;border-color:#758291}
  button:active{transform:translateY(1px)}
  button:disabled{opacity:.5;cursor:default}
  :focus-visible{outline:2px solid var(--accent);outline-offset:3px}
  input[type=checkbox]{width:15px;height:15px;accent-color:var(--accent)}
  header{min-height:76px;padding:14px 24px;gap:20px;background:#1e2125}
  .brand{display:flex;align-items:center;gap:10px;background:none;border:0;padding:0;white-space:nowrap}
  .brand:hover{background:none}
  .brand-mark{display:grid;place-items:center;width:35px;height:39px;border-radius:11px;background:var(--accent);color:#202a35;font:700 17px var(--mono);transform:rotate(-6deg)}
  .brand-name{font-size:19px;font-weight:650;letter-spacing:-.8px}
  .brand-name em{font-family:Georgia,serif;font-weight:400;color:var(--accent)}
  .review-context{min-width:0;flex:1;border-left:1px solid var(--line);padding-left:20px}
  .eyebrow{color:var(--dim);font-size:10px;font-weight:600;letter-spacing:.15em;text-transform:uppercase}
  header .rng{display:block;max-width:100%;overflow:hidden;text-overflow:ellipsis;margin-top:3px;background:none;border:0;padding:0;text-align:left;font-size:11px}
  .top-progress{width:130px;flex-shrink:0;display:flex;flex-direction:column;gap:7px;font-size:11px;color:var(--dim)}
  .bar{flex:none;min-width:0;width:100%;height:4px}
  .bar>i{background:var(--accent)}
  .primary,#next{background:var(--accent);border-color:var(--accent);color:#202a35;font-weight:650}
  .primary:hover,#next:hover{background:#c4d4e5;box-shadow:0 3px 16px #a9bfd820}
  #wrap{padding:16px;gap:16px}
  aside{width:285px;border:1px solid var(--line);border-radius:14px;display:flex;flex-direction:column;overflow:hidden}
  aside>.head{position:static;padding:17px 16px;border:0;font-size:10px;letter-spacing:.1em}
  #cct{font-size:10px;letter-spacing:0;text-transform:none}
  .file-search{margin:0 12px 12px;position:relative}
  .file-search input{width:100%;font-family:inherit;font-size:12px;line-height:1.5;color:var(--fg);background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:9px 12px}
  .file-search input::placeholder{color:var(--dim)}
  #list{overflow:auto;flex:1;padding:0 7px 10px}
  .f{margin:3px 0;padding:11px 9px;gap:10px;border:1px solid transparent;border-radius:8px}
  .f.sel{background:#2c3642;border-color:#536477;box-shadow:none}
  .f.done .nm{text-decoration:none;color:var(--dim)}
  .f .nm{font-size:12px;word-break:normal;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .f .dir{color:var(--dim);font:10px/1.5 var(--mono);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;margin-top:2px}
  .f .sub{margin-top:5px;font-size:10px}
  .f.ov{margin-bottom:14px;padding:13px 12px}
  .f.ov .nm{font-family:inherit;font-size:13px;font-weight:600}
  .shortcuts{flex:0 0 auto;display:flex;align-items:center;justify-content:center;flex-wrap:wrap;gap:6px 16px;padding:9px 16px;border-top:1px solid var(--line);background:var(--bg2)}
  .shortcuts button{display:flex;align-items:center;gap:7px;background:transparent;border-color:transparent;font-size:11px;padding:4px 6px;white-space:nowrap}
  .shortcuts button:hover{background:var(--bg3);border-color:var(--line)}
  .shortcuts kbd{color:var(--accent);min-width:21px;text-align:center;padding:1px 5px}
  .shortcuts .compose-hint{color:var(--dim);font-size:10px;white-space:nowrap}
  tr.code-row.current-line td.code{box-shadow:inset 3px 0 var(--accent)}
  main{border:1px solid var(--line);border-radius:14px;background:var(--bg);scrollbar-color:#515b67 transparent}
  .ovw{max-width:1480px;margin:auto;padding:32px}
  .hero{position:relative;display:flex;justify-content:space-between;align-items:center;gap:24px;padding:4px 0 30px}
  .hero h1{font-size:clamp(28px,3vw,43px);line-height:1.15;font-weight:550;letter-spacing:-1.7px;margin:12px 0}
  .hero h1 em{font-family:Georgia,serif;color:var(--accent);font-weight:400}
  .hero p{color:var(--dim);font-size:13px;margin:0 0 20px;max-width:480px;line-height:1.7}
  .hero-art{width:170px;height:170px;flex-shrink:0}
  .hero-actions{display:flex;align-items:center;gap:14px}
  .hero-actions span{font-size:11px;color:var(--dim)}
  .tiles{grid-template-columns:repeat(4,minmax(0,1fr));gap:0;border:1px solid var(--line);border-radius:12px;overflow:hidden;background:var(--bg2)}
  .tile{border:0;border-radius:0;padding:18px 20px;border-right:1px solid var(--line);border-bottom:1px solid var(--line);background:transparent;min-width:0}
  .tile:nth-child(4n){border-right:0}.tile:nth-child(n+5){border-bottom:0}
  .tile .k{font-size:10px;letter-spacing:.09em}
  .tile .v{font:500 28px/1.2 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;letter-spacing:-.8px;margin:9px 0 5px}
  .tile .s{font-size:10px;line-height:1.6;overflow-wrap:anywhere}
  .tile:nth-child(2) .v{color:var(--addln)}.tile:nth-child(3) .v{color:var(--delln)}
  .sec{margin-top:26px}.sec .h{font-size:10px;letter-spacing:.1em;margin-bottom:12px}
  .meter{height:8px;background:var(--bg3)}.meter>i{background:var(--accent);border-radius:9px;transition:width .25s}
  .progress-panel{padding:18px 20px;background:#242a31;border:1px solid #424e5c;border-radius:12px}
  .progress-panel .h{color:var(--fg)}
  .brow{padding:9px 10px;border-bottom:1px solid #30363d;border-radius:0;grid-template-columns:minmax(100px,2fr) minmax(70px,2fr) 110px}
  .brow .p{font-size:11px}.bar2{height:7px;background:#262b31}
  .brow[role=button]:hover{background:var(--bg3);border-radius:6px}
  .brow:not([role=button]){cursor:default}
  .chip{padding:4px 10px}
  .fhead{padding:20px 24px;background:#22262b}.fhead h1{font-size:15px;margin-bottom:12px}
  .fhead .row{gap:16px;font-size:11px}
  .notes{padding:16px 24px;background:#1e2125}.notes .lbl{font-size:10px}
  textarea{padding:10px 12px;background:#191c20;border-radius:8px;line-height:1.65}
  table.diff{font-size:12px;line-height:1.8}td.ln{color:#9fa9b5;background:#20252b}
  tr.hunk td{padding:8px;color:#b4bfcc;background:#2b323c}
  tr.add td.ln{background:#24344d}tr.del td.ln{background:#3b2828}
  tr.cmt td{background:#22262b;padding-top:12px;padding-bottom:12px}
  .bub{padding:10px 12px;border-radius:10px}.acts{margin-top:8px;margin-bottom:8px}
  .who .tools{opacity:.7}.msg:focus-within .who .tools{opacity:1}
  .hint{padding:14px 24px;color:var(--dim);line-height:2}
  .empty{padding:64px 24px}.empty strong{display:block;color:var(--fg);font-size:21px;margin-bottom:8px}
  .toast{position:fixed;bottom:calc(var(--shortcuts-height,100px) + 12px);left:50%;transform:translate(-50%,15px);max-width:calc(100% - 32px);background:var(--accent);color:#202a35;padding:12px 20px;border-radius:12px;box-shadow:0 8px 32px #0006;opacity:0;pointer-events:none;transition:opacity .2s,transform .2s;z-index:10}
  .toast.on{opacity:1;transform:translate(-50%,0)}
  @media(min-width:1500px){aside{width:310px}.ovw{padding:40px 48px}}
  @media(max-width:1150px){header{gap:12px;padding:14px 18px}.top-progress{width:100px}#me{display:none}.opt{font-size:11px}aside{width:245px}.ovw{padding:24px}.hero-art{width:125px;height:145px}.tile{padding:15px 13px}}
  @media(max-width:900px){header{flex-wrap:wrap}.review-context{flex-basis:45%}.top-progress{flex:1}#wrap{gap:10px;padding:10px}aside{width:210px}.hero-art{display:none}.tiles{grid-template-columns:repeat(2,minmax(0,1fr))}.tile:nth-child(2n){border-right:0}.tile:nth-child(n+5){border-bottom:1px solid var(--line)}.tile:nth-child(n+7){border-bottom:0}.legend{font-size:9px;gap:6px}.brow{grid-template-columns:minmax(80px,1fr) 65px 85px;gap:8px}}
  @media(max-width:600px){header{padding:12px;gap:10px}.brand-name{font-size:17px}.review-context{padding-left:10px}.top-progress{display:none}.opt{margin-right:auto}#next,#refresh{font-size:11px;padding:6px 9px}#wrap{flex-direction:column}aside{width:100%;max-height:230px;flex-shrink:0}aside>.head{padding:10px 14px}.file-search{margin-bottom:6px}#list{min-height:65px}.f{padding:7px 9px}.f.ov{margin-bottom:5px}main{flex:1;min-height:0}.ovw{padding:20px}.hero{padding-bottom:24px}.hero h1{font-size:30px}.hero-actions span{display:none}.fhead,.notes{padding:14px}.legend{display:none}col.c-ln{width:35px}tr.cmt td{padding-left:10px}}
  @media(prefers-reduced-motion:reduce){*,*::before,*::after{animation:none!important;transition:none!important}}
</style>
</head>
<body>
<header>
  <button class="brand" id="brand" aria-label="diff & tonic overview"><span class="brand-mark" aria-hidden="true">±</span><span class="brand-name">diff <em>&amp;</em> tonic</span></button>
  <div class="review-context"><div class="eyebrow" id="repo-name">Your review, on the rocks</div><button class="rng" id="rng"></button></div>
  <div class="top-progress"><span id="pct">0 / 0</span><div class="bar"><i id="fill"></i></div></div>
  <label class="opt" id="agwrap" title="flag every new comment for an agent reply">
    <input type="checkbox" id="ag"> Agent replies <span id="agn"></span>
  </label>
  <span class="me" id="me"></span>
  <button id="next" title="Next unreviewed file (u)">Next unreviewed ↗</button>
  <button id="refresh" title="re-run git diff">Refresh</button>
</header>
<div id="wrap">
  <aside>
    <div class="head"><span>Changed files</span><span id="cct"></span></div>
    <div class="file-search"><input id="file-filter" type="search" placeholder="Find a file…" aria-label="Find a file" autocomplete="off"></div>
    <div id="list"></div>
  </aside>
  <main id="main" tabindex="0" aria-label="Review content"><div class="empty">Loading…</div></main>
</div>
<nav class="shortcuts" aria-label="Keyboard shortcuts">
  <button id="key-prev" aria-keyshortcuts="h [" title="Previous file (H or [)"><kbd>H</kbd> Previous</button>
  <button id="key-next" aria-keyshortcuts="l ]" title="Next file (L or ])"><kbd>L</kbd> Next</button>
  <button id="key-scroll-down" aria-keyshortcuts="j Shift+j" title="Scroll down; Shift+J moves a page"><kbd>J</kbd> Down</button>
  <button id="key-scroll-up" aria-keyshortcuts="k Shift+k" title="Scroll up; Shift+K moves a page"><kbd>K</kbd> Up</button>
  <button id="key-comment" aria-keyshortcuts="i c" title="Comment on the selected line, or the first visible line" disabled><kbd>I</kbd> Comment</button>
  <button id="key-approve" aria-keyshortcuts="o r" disabled><kbd>O</kbd> <span>Approve file</span></button>
  <button id="key-unreviewed" aria-keyshortcuts="u"><kbd>U</kbd> Unreviewed</button>
  <button id="key-overview" aria-keyshortcuts="p"><kbd>P</kbd> Overview</button>
  <span class="compose-hint"><kbd>Shift J / K</kbd> page · <kbd>[ ]</kbd> switch files</span>
  <span class="compose-hint"><kbd>⌘ / Ctrl ↵</kbd> save · <kbd>Esc</kbd> cancel</span>
</nav>
<div class="toast" id="toast" role="status" aria-live="polite"></div>
<script>
const LANG = {js:'javascript',jsx:'javascript',mjs:'javascript',cjs:'javascript',ts:'typescript',tsx:'typescript',
  py:'python',rb:'ruby',go:'go',rs:'rust',java:'java',kt:'kotlin',scala:'scala',swift:'swift',
  c:'c',h:'c',cc:'cpp',cpp:'cpp',hpp:'cpp',cs:'csharp',php:'php',pl:'perl',lua:'lua',sh:'bash',bash:'bash',zsh:'bash',
  sql:'sql',json:'json',yml:'yaml',yaml:'yaml',toml:'ini',ini:'ini',cfg:'ini',xml:'xml',html:'xml',vue:'xml',
  css:'css',scss:'scss',less:'less',md:'markdown',markdown:'markdown',dockerfile:'dockerfile',
  gradle:'groovy',groovy:'groovy',tf:'hcl',hcl:'hcl',ex:'elixir',exs:'elixir',erl:'erlang',clj:'clojure',dart:'dart',r:'r'};

let FILES = [], META = {}, cur = null, open = {};   // open: rowKey -> true (editor visible)
let expanded = {};                                  // resolved threads opened by hand
let ROWS = [];                                      // current file's diff rows

const esc = s => s.replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const $ = s => document.querySelector(s);
let toastTimer;
function toast(message){
  $('#toast').textContent = message;
  $('#toast').classList.add('on');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => $('#toast').classList.remove('on'), 3200);
}
function keyboardClick(el){
  el.tabIndex = 0;
  el.setAttribute('role', 'button');
  el.onkeydown = e => {
    if (e.target !== el || !['Enter', ' '].includes(e.key)) return;
    e.preventDefault(); el.click();
  };
}
function pathLabel(path){
  const parts = path.split('/'), name = parts.pop();
  return '<div class="nm">' + esc(name) + '</div>' +
    (parts.length ? '<div class="dir">' + esc(parts.join('/')) + '/</div>' : '');
}

/* ---- avatars: deterministic inline SVG, nothing fetched ---------------- */
function fnv(s){
  let h = 2166136261 >>> 0;
  for (let i = 0; i < s.length; i++){ h ^= s.charCodeAt(i); h = Math.imul(h, 16777619) >>> 0; }
  return h >>> 0;
}
function humanAvatar(seed, size){
  size = size || 22;
  const h = fnv(seed || 'you'), hue = h % 360, hue2 = (hue + 45 + (h >> 9) % 90) % 360;
  let cells = '';
  for (let y = 0; y < 5; y++) for (let x = 0; x < 3; x++){
    if (!((fnv(seed + ':' + x + ':' + y) >> 3) & 1)) continue;
    for (const xx of (x === 2 ? [2] : [x, 4 - x]))
      cells += '<rect x="' + (xx*4+1) + '" y="' + (y*4+1) + '" width="4" height="4" rx="1.2"/>';
  }
  const id = 'hg' + h;
  return '<svg class="av" width="' + size + '" height="' + size + '" viewBox="0 0 22 22">' +
    '<defs><linearGradient id="' + id + '" x1="0" y1="0" x2="1" y2="1">' +
    '<stop offset="0" stop-color="hsl(' + hue + ' 68% 48%)"/>' +
    '<stop offset="1" stop-color="hsl(' + hue2 + ' 70% 32%)"/></linearGradient></defs>' +
    '<rect width="22" height="22" rx="7" fill="url(#' + id + ')"/>' +
    '<g fill="rgba(255,255,255,.85)">' + cells + '</g></svg>';
}
/* a distinct look for any other author id (an automated reviewer, say) */
function botAvatar(seed, size){
  size = size || 22;
  const h = fnv(seed), hue = 150 + h % 60;            // teal..cyan, seeded
  const id = 'bg' + h;
  return '<svg class="av bot" width="' + size + '" height="' + size + '" viewBox="0 0 22 22">' +
    '<defs><linearGradient id="' + id + '" x1="0" y1="0" x2="1" y2="1">' +
    '<stop offset="0" stop-color="hsl(' + hue + ' 62% 44%)"/>' +
    '<stop offset="1" stop-color="hsl(' + (hue + 30) % 360 + ' 68% 28%)"/></linearGradient></defs>' +
    '<rect width="22" height="22" rx="7" fill="url(#' + id + ')"/>' +
    '<g fill="none" stroke="#fff" stroke-opacity=".93" stroke-width="2" stroke-linecap="round">' +
    '<circle cx="9.6" cy="9.6" r="4.1"/><path d="M12.9 12.9 16.6 16.6"/></g></svg>';
}

function agentAvatar(size){
  size = size || 22;
  return '<svg class="av agent" width="' + size + '" height="' + size + '" viewBox="0 0 22 22">' +
    '<defs><linearGradient id="agrad" x1="0" y1="0" x2="1" y2="1">' +
    '<stop offset="0" stop-color="#b5c8db"/><stop offset=".5" stop-color="#91acc6"/>' +
    '<stop offset="1" stop-color="#6b8eaf"/></linearGradient></defs>' +
    '<rect width="22" height="22" rx="7" fill="url(#agrad)"/>' +
    '<path d="M11 3.4 12.35 8.6 17.2 6.6 14.1 11 17.2 15.4 12.35 13.4 11 18.6 9.65 13.4 4.8 15.4 7.9 11 4.8 6.6 9.65 8.6Z" ' +
    'fill="#fff" fill-opacity=".93"/></svg>';
}
/* three roles: you, the agent you talk to, and any other (bot) author id */
function kindOf(a){ return !a || a === 'you' ? 'you' : a === 'agent' ? 'agent' : 'bot'; }

function avatarFor(a){
  const k = kindOf(a);
  return k === 'agent' ? agentAvatar()
       : k === 'bot'   ? botAvatar(a)
       :                 humanAvatar((META.user||{}).seed || 'you');
}

function nameFor(a){
  const k = kindOf(a);
  if (k === 'you') return 'You';           // never the git identity
  if (k === 'agent') return 'Agent';
  return a.split(/[-_\s]+/).filter(Boolean)
    .map(w => w.length <= 2 ? w.toUpperCase() : w[0].toUpperCase() + w.slice(1))
    .join(' ');
}

const tagFor = a => kindOf(a) === 'agent' ? '<span class="tag">agent</span>'
                  : kindOf(a) === 'bot'   ? '<span class="tag bot">ai</span>' : '';

function ago(ts){
  if (!ts) return '';
  const d = Math.max(0, Math.floor(Date.now()/1000 - ts));
  if (d < 45) return 'just now';
  if (d < 3600) return Math.round(d/60) + 'm ago';
  if (d < 86400) return Math.round(d/3600) + 'h ago';
  return Math.round(d/86400) + 'd ago';
}

function langFor(path){
  const base = path.split('/').pop().toLowerCase();
  let ext = base.includes('.') ? base.split('.').pop() : base;
  if (base.startsWith('dockerfile')) ext = 'dockerfile';
  const l = LANG[ext];
  return (l && window.hljs && hljs.getLanguage(l)) ? l : null;
}
function hl(text, lang){
  if (!lang || !window.hljs) return esc(text);
  try { return hljs.highlight(text, {language: lang, ignoreIllegals: true}).value; }
  catch (e) { return esc(text); }
}

async function api(url, body){
  const r = await fetch(url, body ? {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)} : undefined);
  const j = await r.json();
  if (!r.ok || j.error) throw new Error(j.error || r.statusText);
  return j;
}

const fileOf = p => FILES.find(f => f.path === p);

async function save(path, patch){
  const f = fileOf(path);
  const wasReviewed = f.state.reviewed;
  Object.assign(f.state, patch);          // optimistic
  renderTop(); renderList();
  const r = await api('/api/file', {path, patch});
  f.state = r.state;
  renderTop(); renderList();
  if (patch.reviewed && !wasReviewed){
    const t = totals();
    toast(t.n && t.done === t.n ? 'All files reviewed. That deserves a tonic. ✓' :
      '✓ Reviewed · ' + (t.n - t.done) + ' files to go');
  }
  if (cur === null && document.querySelector('.ovw')) showOverview();
  return r.state;
}

/* ------------------------------------------------------------------ top bar */
function renderTop(){
  const t1 = totals();
  const done = t1.done, n = t1.n;
  $('#fill').style.width = n ? (100*done/n) + '%' : '0';
  $('#pct').textContent = done + ' / ' + n + ' reviewed';
  $('#rng').innerHTML = 'git diff <b>' + esc(META.range||'') + '</b>';
  $('#rng').title = 'overall diff stats (p)';
  $('#rng').onclick = showOverview;
  $('#repo-name').textContent = (META.repo || '').split('/').filter(Boolean).pop() || 'Local review';
  const t0 = totals();
  $('#cct').textContent = t0.comments
    ? t0.openThreads + ' open / ' + t0.comments + ' thread' + (t0.comments>1?'s':'') : '';
  const all = FILES.flatMap(f => f.state.comments);
  const waiting = all.filter(x => x.awaiting).length;
  const replies = all.reduce((a,x) => a + (x.replies||[]).length, 0);
  $('#agn').innerHTML = waiting ? '<span class="abadge">' + waiting + ' waiting</span>'
                      : replies ? '<span class="abadge">' + replies + '</span>' : '';
  $('#me').innerHTML = humanAvatar((META.user||{}).seed || 'you', 20);
  $('#me').title = 'you';
  document.title = '(' + done + '/' + n + ') diff & tonic';
  renderShortcuts();
}

function renderShortcuts(){
  const f = cur && fileOf(cur);
  const ready = !!f && !!$('#rv');
  $('#key-approve').disabled = !ready || f.stale;
  $('#key-approve span').textContent = f && f.state.reviewed ? 'Undo approval' : 'Approve file';
  $('#key-comment').disabled = !ready;
  $('#key-prev').disabled = !FILES.length;
  $('#key-next').disabled = !FILES.length;
}

/* ----------------------------------------------------------------- sidebar */
function renderList(){
  const el = $('#list');
  el.innerHTML = '';
  const query = $('#file-filter').value.trim().toLowerCase();
  const t = totals();
  const ov = document.createElement('div');
  ov.className = 'f ov' + (cur === null ? ' sel' : '');
  ov.innerHTML = '<div class="body"><div class="nm">◈ &nbsp; Review overview</div><div class="sub">' +
    '<span>' + t.n + ' files</span><span class="plus">+' + t.adds + '</span>' +
    '<span class="minus">\u2212' + t.dels + '</span></div></div>';
  ov.onclick = showOverview;
  keyboardClick(ov);
  el.appendChild(ov);
  for (const f of FILES){
    if (f.stale || !f.path.toLowerCase().includes(query)) continue;
    const st = f.status[0];
    const div = document.createElement('div');
    div.className = 'f' + (f.path === cur ? ' sel' : '') + (f.state.reviewed ? ' done' : '');
    const nc = f.state.comments.length;
    const nopen = f.state.comments.filter(x => !x.resolved).length;
    div.innerHTML =
      '<input type="checkbox"' + (f.state.reviewed ? ' checked' : '') + '>' +
      '<div class="body">' + pathLabel(f.path) + '<div class="sub">' +
        '<span class="st ' + esc(st) + '">' + esc(f.status) + '</span>' +
        (f.adds === null ? '<span>binary</span>' :
          '<span class="plus">+' + f.adds + '</span><span class="minus">−' + f.dels + '</span>') +
        (nc ? '<span class="cbadge' + (nopen ? '' : ' settled') + '" title="' + nopen +
              ' open of ' + nc + '">' + (nopen ? nopen : '\u2713 ' + nc) + '</span>' : '') +
        (f.state.comments.some(x => x.awaiting) ? '<span class="dot" title="waiting for the agent"></span>' : '') +
        (f.state.comments.reduce((a,x) => a + (x.replies||[]).length, 0)
          ? '<span class="abadge">\u2726 ' + f.state.comments.reduce((a,x) => a + (x.replies||[]).length, 0) + '</span>' : '') +
        (f.state.notes.trim() ? '<span title="has notes">\u{1F4DD}</span>' : '') +
      '</div></div>';
    div.querySelector('input').onclick = ev => {
      ev.stopPropagation();
      save(f.path, {reviewed: ev.target.checked});
    };
    div.onclick = () => select(f.path);
    div.title = f.path;
    div.querySelector('input').setAttribute('aria-label', 'Mark ' + f.path + ' reviewed');
    keyboardClick(div);
    el.appendChild(div);
  }

  const stale = FILES.filter(f => f.stale && f.path.toLowerCase().includes(query));
  if (stale.length){
    const h = document.createElement('div');
    h.className = 'head';
    h.style.top = 'auto';
    h.innerHTML = '<span>Not in the current diff</span><span>' + stale.length + '</span>';
    h.title = 'reverted, committed elsewhere, or outside the range - kept because they carry comments or notes';
    el.appendChild(h);
    for (const f of stale){
      const nopen = f.state.comments.filter(x => !x.resolved).length;
      const div = document.createElement('div');
      div.className = 'f stale' + (f.path === cur ? ' sel' : '');
      div.innerHTML = '<div class="body">' + pathLabel(f.path) + '<div class="sub">' +
        (f.state.comments.length
          ? '<span class="cbadge' + (nopen ? '' : ' settled') + '">' +
            (nopen ? nopen : '\u2713 ' + f.state.comments.length) + '</span>' : '') +
        (f.state.notes.trim() ? '<span title="has notes">\u{1F4DD}</span>' : '') +
        '</div></div>';
      div.onclick = () => select(f.path);
      div.title = f.path;
      keyboardClick(div);
      el.appendChild(div);
    }
  }
  if (!FILES.some(f => f.path.toLowerCase().includes(query))){
    const empty = document.createElement('div');
    empty.className = 'empty';
    empty.textContent = query ? 'No files match your search.' : 'No changed files. All clear.';
    el.appendChild(empty);
  }
}

/* -------------------------------------------------------------- main panel */
async function select(path){
  cur = path; open = {}; expanded = {};
  if (location.hash !== '#f=' + path) history.replaceState(null, '', '#f=' + path);
  renderList();
  const f = fileOf(path);
  $('#main').innerHTML = '<div class="empty">Loading diff…</div>';
  renderShortcuts();
  let rows;
  try { rows = (await api('/api/diff?path=' + encodeURIComponent(path))).rows; }
  catch (e) { $('#main').innerHTML = '<div class="empty">' + esc(String(e)) + '</div>'; return; }
  drawFile(f, rows);
  $('#main').scrollTop = 0;
}

function drawFile(f, rows){
  ROWS = rows;
  const lang = langFor(f.path);
  const m = document.createElement('div');

  const head = document.createElement('div');
  head.className = 'fhead';
  head.innerHTML = '<h1>' + esc(f.path) + (f.old ? ' <span style="color:var(--dim)">← ' + esc(f.old) + '</span>' : '') + '</h1>' +
    '<div class="row"><label><input type="checkbox" id="rv"' + (f.state.reviewed?' checked':'') + '> Reviewed</label>' +
    '<span class="st ' + esc(f.status[0]) + '">' + esc(f.status) + '</span>' +
    (f.adds === null ? '<span>binary</span>' : '<span class="plus">+' + f.adds + '</span><span class="minus">−' + f.dels + '</span>') +
    '<span>' + f.state.comments.filter(x => !x.resolved).length + ' open of ' +
    f.state.comments.length + ' thread(s)</span></div>';
  m.appendChild(head);

  const notes = document.createElement('div');
  notes.className = 'notes';
  notes.innerHTML = '<div class="lbl">File notes <span class="saved" id="nsv">saved</span></div>' +
    '<textarea id="nt" rows="2" placeholder="General notes for this file…"></textarea>';
  m.appendChild(notes);

  const orph = document.createElement('div');   // filled by paintComments
  orph.id = 'orph';
  m.appendChild(orph);

  const tbl = document.createElement('table');
  tbl.className = 'diff';
  // fixed layout takes its widths from the first row, which is a colspan hunk
  // header - so state them explicitly instead
  tbl.innerHTML = '<colgroup><col class="c-ln"><col class="c-ln"><col></colgroup>';
  const tb = document.createElement('tbody');
  rows.forEach((r, i) => {
    const tr = document.createElement('tr');
    if (r.t === 'hunk' || r.t === 'meta'){
      tr.className = r.t;
      tr.innerHTML = '<td colspan="3">' + esc(r.text) + '</td>';
      tb.appendChild(tr);
      return;
    }
    const sign = r.t === 'add' ? '+' : r.t === 'del' ? '−' : ' ';
    tr.className = r.t + ' code-row';
    tr.dataset.k = r.k;
    tr.dataset.i = i;
    tr.innerHTML =
      '<td class="ln">' + (r.old != null ? r.old : '') + '</td>' +
      '<td class="ln">' + (r.new != null ? r.new : '') + '</td>' +
      '<td class="code"><span class="sig">' + sign + '</span> ' + hl(r.text, lang) + '</td>';
    tr.querySelectorAll('.ln').forEach(td => td.onclick = () => toggle(r.k));
    tr.querySelector('.code').onclick = () => {
      document.querySelectorAll('.current-line').forEach(row => row.classList.remove('current-line'));
      tr.classList.add('current-line');
    };
    tr.querySelector('.code').ondblclick = () => toggle(r.k);
    tb.appendChild(tr);
  });
  tbl.appendChild(tb);
  m.appendChild(tbl);

  $('#main').innerHTML = '';
  $('#main').appendChild(m);

  $('#rv').onchange = e => save(f.path, {reviewed: e.target.checked}).then(() => redrawHead(f));
  const ta = $('#nt');
  ta.value = f.state.notes || '';
  let t;
  ta.oninput = () => {
    clearTimeout(t);
    t = setTimeout(async () => {
      await save(f.path, {notes: ta.value});
      const s = $('#nsv'); if (!s) return;
      s.classList.add('on'); setTimeout(() => s.classList.remove('on'), 900);
    }, 400);
  };
  paintComments(f);
  renderShortcuts();
}

function redrawHead(f){
  const cb = $('#rv'); if (cb) cb.checked = f.state.reviewed;
  renderShortcuts();
}

function approveFile(){
  const f = cur && fileOf(cur);
  if (!f || f.stale || !$('#rv')) return;
  save(cur, {reviewed: !f.state.reviewed}).then(() => {
    if (cur === f.path) redrawHead(f);
  });
}

function addComment(){
  if (!cur || !$('#rv')) return;
  const existing = document.querySelector('.cmt-ed textarea');
  if (existing){ existing.focus(); return; }
  const panel = $('#main').getBoundingClientRect();
  const top = Math.max(panel.top, $('.fhead').getBoundingClientRect().bottom);
  const visible = row => {
    const bounds = row.getBoundingClientRect();
    return bounds.bottom > top && bounds.top < panel.bottom;
  };
  const selected = document.querySelector('.current-line');
  const rows = [...document.querySelectorAll('tr.code-row')];
  const row = selected && visible(selected) ? selected : rows.find(visible) || rows[0];
  if (!row){ $('#nt').focus(); toast('No code lines in this file. Add your thoughts in File notes.'); return; }
  document.querySelectorAll('.current-line').forEach(r => r.classList.remove('current-line'));
  row.classList.add('current-line');
  toggle(row.dataset.k);
}

/* --------------------------------------------------------------- comments */
function toggle(k){
  open[k] = !open[k];
  paintComments(fileOf(cur));
  if (open[k]){
    const ta = document.querySelector('tr.cmt[data-k="' + CSS.escape(k) + '"] textarea');
    if (ta) ta.focus();
  }
}

/* is the user mid-edit? then live polling must not repaint over them */
function busy(){
  return !!document.querySelector('.cmt-ed') ||
         (document.activeElement && document.activeElement.tagName === 'TEXTAREA');
}

function msg(author, text, ts, tools, isReply){
  const d = document.createElement('div');
  d.className = 'msg' + (isReply ? ' reply' : '') + ' ' + kindOf(author);
  d.innerHTML = avatarFor(author) +
    '<div class="bub"><div class="who"><b>' + esc(nameFor(author)) + '</b>' +
    tagFor(author) +
    '<span>' + ago(ts) + '</span><span class="tools"></span></div>' +
    '<div class="txt">' + esc(text) + '</div></div>';
  const bar = d.querySelector('.tools');
  for (const [label, fn] of tools || []){
    const b = document.createElement('button');
    b.textContent = label;
    b.onclick = fn;
    bar.appendChild(b);
  }
  return d;
}

/* Where a comment should hang.
   The stored line number is only trusted when the line still holds the text it
   was written about - otherwise an inserted line above would silently move the
   comment onto unrelated code. Content wins; if the text is gone, so is the
   anchor, and the thread goes to the orphan block instead of somewhere wrong. */
function anchor(c, byKey){
  const snip = c.snippet || '';
  const row = byKey[c.line];
  if (row && (!snip || row.text === snip)) return {key: c.line};
  if (snip.trim().length >= 3){                 // shorter is too generic to chase
    const want = Number(c.line.slice(1)) || 0;
    let best = null;
    for (const r of ROWS){
      if (!r.k || r.text !== snip) continue;
      if (r.k[0] !== c.line[0] && !(c.line[0] === 'R' && r.t === 'ctx')) continue;
      const d = Math.abs((Number(r.k.slice(1)) || 0) - want);
      if (!best || d < best.d) best = {key: r.k, d: d};
    }
    if (best) return {key: best.key, from: c.line};
  }
  return null;
}

function paintComments(f){
  document.querySelectorAll('tr.cmt').forEach(tr => tr.remove());
  const byKey = {};
  for (const r of ROWS) if (r.k) byKey[r.k] = r;

  const byLine = {}, orphans = [];
  f.state.comments.forEach((c, idx) => {
    const a = anchor(c, byKey);
    if (!a){ orphans.push({c, idx}); return; }
    (byLine[a.key] = byLine[a.key] || []).push({c, idx, from: a.from});
  });

  const ob = $('#orph');
  if (ob){
    ob.innerHTML = '';
    ob.className = orphans.length ? 'orph' : '';
    if (orphans.length){
      const h = document.createElement('div');
      h.className = 'oh';
      h.textContent = orphans.length + ' comment' + (orphans.length > 1 ? 's' : '') +
        ' on lines that are no longer in this diff';
      ob.appendChild(h);
      for (const {c, idx} of orphans){
        const t = thread(f, c.line, c, idx, ob);
        const why = byKey[c.line] ? 'line ' + c.line + ' now holds different code'
                                  : 'line ' + c.line + ' is not in this diff';
        t.querySelector('.who b').insertAdjacentHTML('afterend',
          '<span class="moved" title="' + esc(why) + '">was ' + esc(c.line) + '</span>');
        ob.appendChild(t);
      }
    }
  }

  document.querySelectorAll('tr.code-row').forEach(tr => {
    const k = tr.dataset.k;
    const items = byLine[k] || [];
    tr.classList.toggle('has', items.length > 0);
    tr.classList.toggle('settled', items.length > 0 && items.every(x => x.c.resolved));
    if (!items.length && !open[k]) return;

    const row = document.createElement('tr');
    row.className = 'cmt';
    row.dataset.k = k;
    const td = document.createElement('td');
    td.colSpan = 3;
    row.appendChild(td);

    for (const {c, idx, from} of items){
      const t = thread(f, k, c, idx, td);
      if (from) t.querySelector('.who b').insertAdjacentHTML('afterend',
        '<span class="moved" title="the line moved; matched on its text">moved from ' + esc(from) + '</span>');
      td.appendChild(t);
    }
    if (open[k]) editor(f, k, td, null, '', -1);
    tr.parentNode.insertBefore(row, tr.nextSibling);
  });
}

function thread(f, k, c, idx, td){
  const wrap = document.createElement('div');
  const shut = c.resolved && !expanded[c.id];
  wrap.className = 'thr' + (c.resolved ? ' done' : '');

  const setResolved = async on => {
    await api('/api/resolve', {path: f.path, id: c.id, on});
    expanded[c.id] = false;
    await sync(true);
  };
  const del = () => {
    const arr = f.state.comments.slice();
    arr.splice(idx, 1);
    save(f.path, {comments: arr}).then(() => paintComments(f));
  };
  const tools = c.resolved
    ? [['reopen', () => setResolved(false)]]
    : [['edit', () => editor(f, k, td, head, c.text, idx)], ['delete', del]];
  const head = msg(c.author || 'you', c.text, c.ts, tools, false);
  if (c.resolved){
    head.querySelector('.who b').insertAdjacentHTML('afterend',
      '<span class="ok">\u2713 resolved</span>');
    if (shut){
      head.querySelector('.txt').classList.add('clamp');
      const n = (c.replies || []).length;
      if (n) head.querySelector('.who .tools').insertAdjacentHTML('beforebegin',
        '<span class="more">' + n + ' repl' + (n === 1 ? 'y' : 'ies') + '</span>');
      head.querySelector('.bub').onclick = ev => {
        if (ev.target.tagName === 'BUTTON') return;
        expanded[c.id] = true;
        paintComments(f);
      };
    }
  }
  wrap.appendChild(head);

  if (shut) return wrap;      // collapsed: headline only, click to open

  (c.replies || []).forEach((r, ri) => {
    const el = msg(r.author, r.text, r.ts, [
      ['edit', () => replyEditor(f, c, wrap, el, r.text, ri)],
      ['\u00d7', async () => {
        await api('/api/reply/edit', {path: f.path, id: c.id, index: ri, text: ''});
        await sync(true);
      }],
    ], true);
    wrap.appendChild(el);
  });

  const acts = document.createElement('div');
  acts.className = 'acts';
  if (c.awaiting && !c.resolved){
    const w = document.createElement('span');
    w.className = 'await';
    w.textContent = 'waiting for the agent \u2014 run: review.py pending';
    acts.appendChild(w);
  }
  const reply = document.createElement('button');
  reply.textContent = 'Reply';
  reply.onclick = () => replyEditor(f, c, wrap, null, '', -1);
  acts.appendChild(reply);

  if (!c.resolved){
    const ask = document.createElement('button');
    ask.textContent = c.awaiting ? 'Cancel ask' : 'Ask agent';
    ask.title = 'the agent picks these up with `review.py pending`';
    ask.onclick = async () => {
      await api('/api/ask', {path: f.path, id: c.id, on: !c.awaiting});
      await sync(true);
    };
    acts.appendChild(ask);
  }

  const res = document.createElement('button');
  res.textContent = c.resolved ? 'Reopen' : 'Resolve';
  res.title = c.resolved ? 'put this thread back on the list'
                         : 'keep it visible, but mark it dealt with';
  res.onclick = () => setResolved(!c.resolved);
  acts.appendChild(res);

  if (c.resolved && expanded[c.id]){
    const hide = document.createElement('button');
    hide.textContent = 'Collapse';
    hide.onclick = () => { expanded[c.id] = false; paintComments(f); };
    acts.appendChild(hide);
  }
  wrap.appendChild(acts);
  return wrap;
}

function box(placeholder, text, onSave, onCancel, why){
  const b = document.createElement('div');
  b.className = 'cmt-ed';
  b.innerHTML = '<textarea rows="3"></textarea><div class="btns">' +
    '<button data-a="s">Save</button><button data-a="c">Cancel</button>' +
    '<span class="why">' + esc(why || '') + '</span></div>';
  const ta = b.querySelector('textarea');
  ta.placeholder = placeholder;
  ta.value = text;
  b.querySelector('[data-a="s"]').onclick = () => onSave(ta.value);
  b.querySelector('[data-a="c"]').onclick = onCancel;
  ta.onkeydown = e => {
    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)){ e.preventDefault(); onSave(ta.value); }
    if (e.key === 'Escape') onCancel();
  };
  setTimeout(() => ta.focus(), 0);
  return b;
}

/* new / edited top-level comment */
function editor(f, k, td, replace, text, idx){
  const done = () => { open[k] = false; paintComments(f); };
  const b = box('Comment on ' + k + '\u2026', text, v => {
    const val = v.trim();
    const arr = f.state.comments.slice();
    if (idx >= 0){
      if (val) arr[idx] = Object.assign({}, arr[idx], {text: val}); else arr.splice(idx, 1);
    } else if (val){
      const row = ROWS.find(r => r.k === k);
      arr.push({line: k, text: val, author: 'you', awaiting: !!META.agent_replies,
                snippet: row ? row.text : ''});
    } else return done();
    open[k] = false;
    save(f.path, {comments: arr}).then(() => paintComments(f));
  }, done, META.agent_replies && idx < 0 ? '\u2726 will be flagged for an agent reply' : '');
  if (replace) replace.replaceWith(b); else td.appendChild(b);
}

/* reply inside a thread */
function replyEditor(f, c, wrap, replace, text, ri){
  const b = box('Reply\u2026', text, async v => {
    if (ri >= 0) await api('/api/reply/edit', {path: f.path, id: c.id, index: ri, text: v});
    else if (v.trim()) await api('/api/reply', {path: f.path, id: c.id, text: v, author: 'you'});
    await sync(true);
  }, () => paintComments(f));
  if (replace) replace.replaceWith(b); else wrap.appendChild(b);
}

/* ------------------------------------------------- live state (agent CLI) */
let VER = 0;

async function sync(force){
  if (!force && busy()) return;
  const d = await api('/api/files');
  FILES = d.files; META = d.meta; VER = d.meta.version;
  $('#ag').checked = !!META.agent_replies;
  $('#agwrap').classList.toggle('on', !!META.agent_replies);
  renderTop(); renderList();
  const f = cur && fileOf(cur);
  if (f) paintComments(f);
  else if (cur === null && document.querySelector('.ovw')) showOverview();
}

setInterval(async () => {
  if (busy()) return;
  try {
    const v = (await api('/api/version')).version;
    if (v !== VER) await sync(false);
  } catch (e) { /* server stopped; keep the page usable */ }
}, 2000);

/* --------------------------------------------------------------- overview */
function totals(){
  const cs = FILES.flatMap(f => f.state.comments);
  const live = FILES.filter(f => !f.stale);
  const t = {
    n: live.length,
    stale: FILES.length - live.length,
    done: live.filter(f => f.state.reviewed).length,
    adds: 0, dels: 0, binary: 0,
    comments: cs.length,
    replies: cs.reduce((a,c) => a + (c.replies||[]).length, 0),
    waiting: cs.filter(c => c.awaiting).length,
    resolved: cs.filter(c => c.resolved).length,
    openThreads: cs.filter(c => !c.resolved).length,
    notes: FILES.filter(f => (f.state.notes||'').trim()).length,
    byStatus: {}, byExt: {}, byDirty: {},
  };
  for (const f of live){
    if (f.adds === null){ t.binary++; } else { t.adds += f.adds; t.dels += f.dels; }
    const st = (f.status||'?')[0];
    t.byStatus[st] = (t.byStatus[st] || 0) + 1;
    if (f.dirty) t.byDirty[f.dirty] = (t.byDirty[f.dirty] || 0) + 1;
    const base = f.path.split('/').pop();
    const ext = base.includes('.') ? '.' + base.split('.').pop() : base;
    const e = t.byExt[ext] = t.byExt[ext] || {files:0, adds:0, dels:0};
    e.files++; e.adds += f.adds || 0; e.dels += f.dels || 0;
  }
  return t;
}

const STATUS_NAME = {A:'added', M:'modified', D:'deleted', R:'renamed', C:'copied', T:'type change'};
let ovwAll = false;

function bar(a, d, max, title){
  const w = x => max > 0 ? (100 * x / max).toFixed(2) + '%' : '0';
  return '<div class="bar2" title="' + esc(title) + '">' +
    '<i class="a" style="width:' + w(a) + '"></i>' +
    '<i class="d" style="width:' + w(d) + '"></i></div>';
}

function churnRows(items, max, onClick){
  const wrap = document.createElement('div');
  wrap.className = 'rows';
  for (const it of items){
    const r = document.createElement('div');
    r.className = 'brow';
    r.innerHTML = '<div class="p">' + (it.tick ? '<span class="tick">\u2713</span> ' : '') +
        esc(it.label) + '</div>' +
      (it.binary ? '<div class="nums" style="text-align:left">binary</div>'
                 : bar(it.adds, it.dels, max, it.label + ': +' + it.adds + ' \u2212' + it.dels)) +
      '<div class="nums"><span class="plus">+' + it.adds + '</span> ' +
        '<span class="minus">\u2212' + it.dels + '</span></div>';
    if (onClick){ r.onclick = () => onClick(it); keyboardClick(r); }
    r.title = it.label;
    wrap.appendChild(r);
  }
  return wrap;
}

function sec(title, extraHTML){
  const d = document.createElement('div');
  d.className = 'sec';
  d.innerHTML = '<div class="h"><span>' + esc(title) + '</span>' + (extraHTML || '') + '</div>';
  return d;
}

const LEGEND = '<span class="legend"><span><i class="sw" style="background:var(--c-add)"></i>additions</span>' +
               '<span><i class="sw d" style="background:var(--c-del)"></i>deletions</span></span>';

function showOverview(){
  cur = null;
  renderShortcuts();
  if (location.hash) history.replaceState(null, '', location.pathname);
  renderList();
  const t = totals(), st = META.stats || {commits:0, authors:[]};
  const m = document.createElement('div');
  m.className = 'ovw';

  const tile = (k, v, s) => '<div class="tile"><div class="k">' + k + '</div>' +
    '<div class="v">' + v + '</div><div class="s">' + (s||'') + '</div></div>';
  const pct = t.n ? Math.round(100 * t.done / t.n) : 0;
  const dirtyBits = Object.entries(t.byDirty)
    .map(([k,v]) => v + ' ' + (k === 'untracked' ? 'new' : k)).join(' \u00b7 ');

  const complete = t.n > 0 && t.done === t.n;
  const hero = document.createElement('div');
  hero.className = 'hero';
  hero.innerHTML = '<div><div class="eyebrow">A fresh perspective on your code</div>' +
    '<h1>' + (!t.n ? 'Nothing to stir <em>just yet.</em>' : complete ? 'All reviewed. <em>Cheers.</em>' : 'Big diff. <em>Small sips.</em>') + '</h1>' +
    '<p>' + (!t.n ? 'No changes between these refs. Make a change, then refresh to pour your next review.' :
      complete ? 'Every file checked off. Your notes and conversations are right here whenever you need them.' :
      'A little clarity, one file at a time. Settle in, follow the changes, and leave your thoughts along the way.') + '</p>' +
    (t.n && !complete ? '<div class="hero-actions"><button class="primary" id="continue-review">' +
      (t.done ? 'Continue review' : 'Start reviewing') + ' ↗</button><span>' + (t.n-t.done) + ' files to explore · <kbd>u</kbd> to jump in</span></div>' : '') +
    '</div><svg class="hero-art" viewBox="0 0 180 180" aria-hidden="true">' +
    '<circle cx="91" cy="91" r="76" fill="#252c34"/><circle cx="91" cy="91" r="75" fill="none" stroke="#475566" stroke-dasharray="2 7"/>' +
    '<g transform="rotate(9 90 90)"><path d="M60 54h66l-8 99H68Z" fill="#2d3743" stroke="#b6c5d6" stroke-width="2"/>' +
    '<path d="M65 92q16-9 29 0t27 0l-5 57H70Z" fill="#a9bfd8" fill-opacity=".18"/>' +
    '<rect x="75" y="75" width="23" height="23" rx="5" fill="#a9bfd8" fill-opacity=".15" stroke="#8498af" transform="rotate(-14 86 86)"/>' +
    '<rect x="90" y="107" width="22" height="22" rx="5" fill="#a9bfd8" fill-opacity=".12" stroke="#8498af" transform="rotate(17 101 118)"/>' +
    '<path d="m100 91 17-59h15" fill="none" stroke="#a9bfd8" stroke-width="4" stroke-linecap="round"/>' +
    '<circle cx="62" cy="57" r="24" fill="#a9bfd8" stroke="#1e2731" stroke-width="3"/>' +
    '<circle cx="62" cy="57" r="18" fill="none" stroke="#5d7896"/>' +
    '<path d="M62 39v36M44 57h36M49 44l26 26M49 70l26-26" stroke="#5d7896" stroke-width="1.5"/>' +
    '<circle cx="81" cy="120" r="2" fill="#a9bfd8"/><circle cx="104" cy="141" r="2" fill="#a9bfd8"/>' +
    '</g><path d="M143 64h12m-6-6v12M34 107h10m-5-5v10" stroke="#a9bfd8" stroke-width="2" stroke-linecap="round"/></svg>';
  m.appendChild(hero);

  const tiles = document.createElement('div');
  tiles.className = 'tiles';
  tiles.innerHTML =
    tile('Files changed', t.n, Object.entries(t.byStatus)
        .map(([k,v]) => v + ' ' + (STATUS_NAME[k]||k)).join(' \u00b7 ') +
        (t.stale ? ' \u00b7 ' + t.stale + ' no longer in the diff' : '')) +
    tile('Additions', '<i class="sw" style="background:var(--c-add)"></i>+' + t.adds,
         t.binary ? t.binary + ' binary file(s) not counted' : 'lines') +
    tile('Deletions', '<i class="sw d" style="background:var(--c-del)"></i>\u2212' + t.dels, 'lines') +
    tile('Net', (t.adds - t.dels >= 0 ? '+' : '\u2212') + Math.abs(t.adds - t.dels),
         (t.adds + t.dels) + ' lines of churn') +
    tile('Commits', st.commits, st.authors.length
         ? st.authors.length + ' author' + (st.authors.length > 1 ? 's' : '') : 'in ' + esc(META.range||'')) +
    tile('Reviewed', t.done + ' / ' + t.n, pct + '% \u00b7 ' + (t.n - t.done) + ' to go') +
    tile('Threads', t.openThreads + ' / ' + t.comments,
         'open \u00b7 ' + t.resolved + ' resolved \u00b7 ' + t.replies + ' repl' +
         (t.replies === 1 ? 'y' : 'ies') +
         (t.waiting ? ' \u00b7 ' + t.waiting + ' waiting' : '')) +
    tile('Uncommitted', META.worktree ? Object.values(t.byDirty).reduce((a,b)=>a+b,0) : '\u2014',
         META.worktree ? (dirtyBits || 'working tree clean') : 'excluded (--no-worktree)');
  m.appendChild(tiles);

  const prog = sec('Review progress');
  prog.classList.add('progress-panel');
  const meter = document.createElement('div');
  meter.className = 'meter';
  meter.innerHTML = '<i style="width:' + pct + '%"></i>';
  meter.title = t.done + ' of ' + t.n + ' files reviewed';
  prog.appendChild(meter);
  const plab = document.createElement('div');
  plab.className = 'nums';
  plab.style.cssText = 'text-align:left;margin-top:5px';
  plab.textContent = t.done + ' of ' + t.n + ' files reviewed \u00b7 ' + pct + '%' +
    (t.waiting ? ' \u00b7 ' + t.waiting + ' thread(s) waiting on the agent' : '');
  prog.appendChild(plab);
  m.appendChild(prog);

  if (st.authors.length){
    const asec = sec('Commits by author');
    const chips = document.createElement('div');
    chips.className = 'chips';
    chips.innerHTML = st.authors.slice(0, 8)
      .map(a => '<span class="chip">' + esc(a.name) + ' \u00b7 ' + a.commits + '</span>').join('');
    asec.appendChild(chips);
    m.appendChild(asec);
  }

  // per-file churn, biggest first
  const files = FILES.filter(f => !f.stale).map(f => ({label: f.path, adds: f.adds||0, dels: f.dels||0,
                                 binary: f.adds === null, tick: f.state.reviewed, path: f.path}))
                     .sort((a,b) => (b.adds+b.dels) - (a.adds+a.dels));
  const shown = ovwAll ? files : files.slice(0, 15);
  const more = files.length - shown.length;
  const fsec = sec('Churn by file' + (more ? ' \u2014 top ' + shown.length + ' of ' + files.length : ''),
                   (files.length > 15 ? '<button id="ovall">' + (ovwAll ? 'Show top 15' : 'Show all ' + files.length) + '</button>' : '') + LEGEND);
  const fmax = Math.max(1, ...files.map(f => f.adds + f.dels));
  fsec.appendChild(churnRows(shown, fmax, it => select(it.path)));
  m.appendChild(fsec);

  // per-extension churn
  const exts = Object.entries(t.byExt)
    .map(([k,v]) => ({label: k + '  (' + v.files + ' file' + (v.files>1?'s':'') + ')',
                      adds: v.adds, dels: v.dels}))
    .sort((a,b) => (b.adds+b.dels) - (a.adds+a.dels)).slice(0, 10);
  const esec = sec('Churn by file type', LEGEND);
  esec.appendChild(churnRows(exts, Math.max(1, ...exts.map(e => e.adds + e.dels)), null));
  m.appendChild(esec);

  $('#main').innerHTML = '';
  $('#main').appendChild(m);
  const start = $('#continue-review');
  if (start) start.onclick = nextUnreviewed;
  const btn = $('#ovall');
  if (btn) btn.onclick = () => { ovwAll = !ovwAll; showOverview(); };
  $('#main').scrollTop = 0;
}

/* ------------------------------------------------------------ nav + boot */
function step(d){
  if (!FILES.length) return;
  let i = FILES.findIndex(f => f.path === cur);
  i = Math.max(0, Math.min(FILES.length - 1, (i < 0 ? 0 : i + d)));
  select(FILES[i].path);
}
function nextUnreviewed(){
  const i = FILES.findIndex(f => f.path === cur);
  const order = FILES.slice(i + 1).concat(FILES.slice(0, i + 1));
  const t = order.find(f => !f.stale && !f.state.reviewed);
  if (t) select(t.path);
  else { showOverview(); toast(totals().n ? 'All files reviewed. That deserves a tonic. ✓' : 'No changed files. All clear.'); }
}

function scrollReview(key){
  const panel = $('#main');
  const page = Math.max(64, panel.clientHeight - ($('.fhead')?.offsetHeight || 0) - 32);
  const distance = {ArrowDown:64, ArrowUp:-64, PageDown:page, PageUp:-page};
  if (Object.hasOwn(distance, key)) panel.scrollBy({top:distance[key], behavior:'instant'});
  else if (key === 'Home') panel.scrollTo({top:0, behavior:'instant'});
  else if (key === 'End') panel.scrollTo({top:panel.scrollHeight, behavior:'instant'});
  else return false;
  return true;
}

document.addEventListener('keydown', e => {
  const t = e.target.tagName;
  if (t === 'TEXTAREA' || t === 'INPUT' || t === 'SELECT' || e.target.isContentEditable ||
      e.metaKey || e.ctrlKey || e.altKey || e.defaultPrevented || e.isComposing) return;
  const key = e.key.toLowerCase();
  if (key === 'j' || key === 'k'){
    e.preventDefault();
    scrollReview(key === 'j' ? (e.shiftKey ? 'PageDown' : 'ArrowDown')
                            : (e.shiftKey ? 'PageUp' : 'ArrowUp'));
    return;
  }
  if (!e.shiftKey && scrollReview(e.key)){ e.preventDefault(); return; }
  if (e.repeat) return;
  const action = {l: () => step(1), h: () => step(-1), ']': () => step(1), '[': () => step(-1),
                  u: nextUnreviewed, p: showOverview, o: approveFile, i: addComment,
                  r: approveFile, c: addComment}[key];
  if (action){ e.preventDefault(); action(); }
});
$('#ag').onchange = async e => {
  await api('/api/option', {agent_replies: e.target.checked});
  await sync(true);
};
$('#next').onclick = nextUnreviewed;
$('#key-prev').onclick = () => step(-1);
$('#key-next').onclick = () => step(1);
$('#key-scroll-down').onclick = () => scrollReview('ArrowDown');
$('#key-scroll-up').onclick = () => scrollReview('ArrowUp');
$('#key-approve').onclick = approveFile;
$('#key-comment').onclick = addComment;
$('#key-unreviewed').onclick = nextUnreviewed;
$('#key-overview').onclick = showOverview;
if (window.ResizeObserver){
  new ResizeObserver(([entry]) => {
    document.documentElement.style.setProperty('--shortcuts-height', entry.target.offsetHeight + 'px');
  }).observe($('.shortcuts'));
}
$('#brand').onclick = showOverview;
$('#file-filter').oninput = renderList;
$('#refresh').onclick = async () => {
  const button = $('#refresh');
  button.disabled = true; button.textContent = 'Refreshing…';
  try {
    const d = await api('/api/refresh', {});
    FILES = d.files; META = d.meta;
    renderTop(); renderList();
    if (cur && fileOf(cur)) await select(cur); else showOverview();
    toast('Fresh diff, ready to review.');
  } catch(e){ toast('Could not refresh: ' + e.message); }
  finally { button.disabled = false; button.textContent = 'Refresh'; }
};

(async () => {
  const d = await api('/api/files');
  FILES = d.files; META = d.meta; VER = d.meta.version;
  $('#ag').checked = !!META.agent_replies;
  $('#agwrap').classList.toggle('on', !!META.agent_replies);
  renderTop(); renderList();
  const want = decodeURIComponent((location.hash.match(/^#f=(.*)$/) || [])[1] || '');
  if (want && fileOf(want)) select(want);
  else showOverview();
})();
</script>
</body>
</html>
"""

# ------------------------------------------------- agent-facing subcommands

def thread_context(rows, line, n):
    """The diff lines around a commented line, with the target marked."""
    i = next((k for k, r in enumerate(rows) if r.get("k") == line), None)
    if i is None:
        return []
    hunk = next((rows[k]["text"] for k in range(i, -1, -1)
                 if rows[k]["t"] == "hunk"), None)
    sign = {"add": "+", "del": "-", "ctx": " "}
    out = [hunk] if hunk else []
    for k in range(max(0, i - n), min(len(rows), i + n + 1)):
        r = rows[k]
        if r["t"] == "hunk":
            continue
        out.append("%s%s%s" % (">>> " if k == i else "    ",
                               sign.get(r["t"], " "), r.get("text", "")))
    return out


def open_review(repo_arg):
    """(root, store, entries-by-path) for an existing .review/state.json."""
    root = repo_root(repo_arg)
    store = Store(root)
    d = store.data
    if not d.get("range"):
        raise SystemExit("no review found in %s/.review/state.json - start the "
                         "server once first" % root)
    entries = {e["path"]: e for e in
               collect_files(root, d.get("base"), d["range"],
                             worktree=bool(d.get("worktree", True)),
                             keep=stateful_paths(store))}
    return root, store, entries


def cmd_pending(argv):
    ap = argparse.ArgumentParser(
        prog="review.py pending",
        description="Print review comments awaiting an agent reply, as JSON.")
    ap.add_argument("--repo", default=os.getcwd())
    ap.add_argument("--all", action="store_true",
                    help="every comment, not just the ones flagged for the agent")
    ap.add_argument("--context", type=int, default=6,
                    help="diff lines of context around each comment (default 6)")
    a = ap.parse_args(argv)

    root, store, entries = open_review(a.repo)
    rows_cache, out = {}, []
    for path in sorted(store.data["files"]):
        f = store.file(path)
        wanted = [c for c in f["comments"]
                  if a.all or (c.get("awaiting") and not c.get("resolved") and
                               not any(r["author"] != "you" for r in c["replies"]))]
        if not wanted:
            continue
        if path not in rows_cache:
            e = entries.get(path)
            try:
                rows_cache[path] = file_diff(root, e) if e else []
            except GitError:
                rows_cache[path] = []
        for c in wanted:
            out.append({"path": path, "id": c["id"], "line": c["line"],
                        "side": "new" if c["line"].startswith("R") else "old",
                        "lineno": c["line"][1:], "author": c["author"],
                        "comment": c["text"], "replies": c["replies"],
                        "resolved": c.get("resolved", False),
                        "file_notes": f["notes"], "reviewed": f["reviewed"],
                        "context": thread_context(rows_cache[path], c["line"],
                                                  a.context)})
    json.dump(out, sys.stdout, indent=2)
    sys.stdout.write("\n")


def cmd_reply(argv):
    ap = argparse.ArgumentParser(
        prog="review.py reply",
        description="Reply to a review comment (shows up in the browser live).")
    ap.add_argument("path", nargs="?", help="file path the comment is on")
    ap.add_argument("id", nargs="?", help="comment id from `review.py pending`")
    ap.add_argument("text", nargs="?", help="the reply")
    ap.add_argument("--repo", default=os.getcwd())
    ap.add_argument("--author", default="agent",
                    help='who is replying: "you", "agent", or any other id '
                         '(e.g. ai-review-agent), which renders as its own bot')
    ap.add_argument("--batch", metavar="FILE",
                    help='JSON list of {path,id,text}; "-" reads stdin')
    a = ap.parse_args(argv)

    if a.batch:
        raw = sys.stdin.read() if a.batch == "-" else open(a.batch).read()
        items = json.loads(raw)
    elif a.path and a.id and a.text:
        items = [{"path": a.path, "id": a.id, "text": a.text}]
    else:
        raise SystemExit("give path, id and text - or --batch FILE")

    store = Store(repo_root(a.repo))
    for it in items:
        store.add_reply(it["path"], it["id"], it["text"],
                        it.get("author", a.author))
    print("wrote %d repl%s to %s" % (len(items), "y" if len(items) == 1 else "ies",
                                     store.path))


AUTHOR_LABEL = {"you": "you (human reviewer)", "agent": "agent (that's you)"}


def cmd_todo(argv):
    ap = argparse.ArgumentParser(
        prog="review.py todo",
        description="Plain-text list of the review threads to address.")
    ap.add_argument("--repo", default=os.getcwd())
    ap.add_argument("--all", action="store_true", help="include resolved threads")
    ap.add_argument("--flagged", action="store_true",
                    help="only threads flagged for an agent reply")
    ap.add_argument("-c", "--context", type=int, default=0, metavar="N",
                    help="also print N diff lines around each comment")
    a = ap.parse_args(argv)

    root, store, entries = open_review(a.repo)
    d = store.data
    def wrap(t, ind):
        return "\n".join(textwrap.fill(para.strip(), width=96, initial_indent=ind,
                                       subsequent_indent=ind)
                          for para in (t or "").split("\n") if para.strip())

    shown = hidden = flagged = 0
    out = []
    for path in sorted(d["files"]):
        f = store.file(path)
        threads = [c for c in f["comments"]
                   if (a.all or not c.get("resolved")) and
                      (not a.flagged or c.get("awaiting"))]
        hidden += len([c for c in f["comments"] if c.get("resolved")]) if not a.all else 0
        if not threads and not (f["notes"].strip() and not a.flagged):
            continue

        rows = []
        e = entries.get(path)
        if e:
            try:
                rows = file_diff(root, e)
            except GitError:
                rows = []
        keys = {r["k"] for r in rows if r.get("k")}

        out.append("")
        out.append("%s%s" % (path, "   [not in the current diff]" if (e or {}).get("stale") else ""))
        if f["notes"].strip():
            out.append(wrap("notes: " + f["notes"].strip(), "  "))
        for c in threads:
            shown += 1
            marks = []
            if c.get("awaiting"):
                marks.append("FLAGGED-FOR-YOU")
                flagged += 1
            if c.get("resolved"):
                marks.append("resolved")
            if c["line"] not in keys:
                marks.append("line no longer in the diff")
            out.append("  [%s] %s  by %s%s" % (
                c["id"], c["line"], c.get("author", "you"),
                ("  <" + ", ".join(marks) + ">") if marks else ""))
            out.append(wrap(c["text"], "      "))
            if a.context and rows:
                for ln in thread_context(rows, c["line"], a.context):
                    out.append("      | " + ln)
            for r in c["replies"]:
                out.append(wrap("-> %s: %s" % (r["author"], r["text"]), "      "))

    live = [e for e in entries.values() if not e.get("stale")]
    done = sum(1 for e in live if store.file(e["path"])["reviewed"])
    print("review %s in %s" % (d.get("range"), root))
    print("%d thread(s) to address, %d flagged for you%s" % (
        shown, flagged, ", %d resolved (hidden)" % hidden if hidden else ""))
    print("%d of %d changed files marked reviewed" % (done, len(live)))
    print("\n".join(out))
    print("""
authors you will see:
  you              the human reviewer - their questions are what you answer
  agent            you, the assistant working this queue
  ai-review-agent  a separate automated reviewer; its findings are suggestions,
                   not requests from the human - address or push back on them

to respond (from %s):
  python3 review.py reply <file> <comment-id> "your answer"
  python3 review.py resolve <file> <comment-id>      # once it is actually done
  python3 review.py pending -c 8                     # same queue as JSON + diff context""" % root)


def cmd_resolve(argv):
    ap = argparse.ArgumentParser(
        prog="review.py resolve",
        description="Mark a comment thread resolved (it stays visible, dimmed).")
    ap.add_argument("path", help="file path the comment is on")
    ap.add_argument("id", help="comment id from `review.py pending`")
    ap.add_argument("--repo", default=os.getcwd())
    ap.add_argument("--reopen", action="store_true", help="unresolve instead")
    a = ap.parse_args(argv)
    store = Store(repo_root(a.repo))
    c = store.set_resolved(a.path, a.id, not a.reopen)
    print("%s %s on %s" % ("reopened" if a.reopen else "resolved", c["id"], a.path))


# ------------------------------------------------------------------------ main

def main():
    ap = argparse.ArgumentParser(
        description="Local git diff review tool (state in .review/state.json).")
    ap.add_argument("base", nargs="?", default="main", help="base ref (default: main)")
    ap.add_argument("head", nargs="?", default=None, help="head ref (default: current branch)")
    ap.add_argument("--repo", default=os.getcwd(), help="repo path (default: cwd)")
    ap.add_argument("--port", type=int, default=8420)
    ap.add_argument("--merge-base", action="store_true",
                    help="use base...head (diff from merge base)")
    ap.add_argument("--worktree", dest="worktree", action="store_true", default=None,
                    help="include uncommitted changes (default: auto)")
    ap.add_argument("--no-worktree", dest="worktree", action="store_false",
                    help="committed range only")
    ap.add_argument("--no-untracked", dest="untracked", action="store_false",
                    default=True, help="skip untracked files")
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args()

    try:
        root = repo_root(args.repo)
    except (GitError, FileNotFoundError) as e:
        raise SystemExit("error: %s is not a git repo (%s)" % (args.repo, e))

    head = args.head or current_branch(root)
    verify_ref(root, args.base)
    verify_ref(root, head)
    rng = "%s%s%s" % (args.base, "..." if args.merge_base else "..", head)

    # uncommitted work only makes sense when head is what is checked out
    at_head = git(root, "rev-parse", head + "^{commit}").strip() == \
        git(root, "rev-parse", "HEAD^{commit}").strip()
    worktree = at_head if args.worktree is None else args.worktree
    if worktree and not at_head:
        print("note: %s is not the checked-out commit; uncommitted changes are "
              "diffed against %s anyway" % (head, args.base))

    store = Store(root)
    store.meta(base=args.base, head=head, range=rng, repo=root, worktree=worktree)
    if ensure_gitignore(root):
        print("added .review/ to %s/.gitignore" % root)

    files = collect_files(root, args.base, rng, worktree=worktree,
                          untracked=args.untracked, keep=stateful_paths(store))
    Handler.ctx = {"root": root, "base": args.base, "head": head,
                   "range": rng, "files": files, "store": store,
                   "worktree": worktree, "untracked": args.untracked,
                   "user": git_user(root), "stats": range_stats(root, rng)}

    try:
        srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError as e:
        raise SystemExit("error: cannot bind port %d (%s) - try --port" % (args.port, e))
    srv.daemon_threads = True

    url = "http://127.0.0.1:%d/" % args.port
    done = sum(1 for f in files if store.file(f["path"])["reviewed"])
    dirty = sum(1 for f in files if f.get("dirty"))
    print("repo   %s" % root)
    print("diff   git diff %s  (%d files, %d already reviewed)" % (rng, len(files), done))
    print("dirty  %s" % ("%d file(s) with uncommitted changes included" % dirty
                         if worktree else "uncommitted changes excluded"))
    print("state  %s" % store.path)
    print("serving %s   (ctrl-c to stop)" % url)
    sys.stdout.flush()
    if not args.no_open:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye - progress saved in %s" % store.path)


SUBCOMMANDS = {"todo": cmd_todo, "pending": cmd_pending, "reply": cmd_reply,
               "resolve": cmd_resolve}

if __name__ == "__main__":
    argv = sys.argv[1:]
    if argv and argv[0] in SUBCOMMANDS:
        SUBCOMMANDS[argv[0]](argv[1:])
    else:
        main()
