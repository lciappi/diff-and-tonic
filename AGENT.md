# diff & tonic — instructions for an agent

A human is reviewing a diff with **diff & tonic** (`review.py`, see
[README.md](README.md)). Their comments live in `<repo>/.review/state.json`. Your job
is to answer them and do the work they ask for.

Everything goes through the script. **Never edit `state.json` by hand** — the page
watches that file and the script keeps ids, threads and resolved state consistent.

## 1. See what to address

```bash
python3 /path/to/review.py todo
```

```
review main..feature in /path/to/repo
7 thread(s) to address, 4 flagged for you, 25 resolved (hidden)
35 of 39 changed files marked reviewed

src/main/java/…/MonoovaIdentityMatchResultMapper.java
  [b1f07a24] R219  by ai-review-agent
      HIGH — the substring match on "NOT_FOUND" turns a PB-side misconfiguration into
      a decisive customer rejection. …
  [40981d7e] R175  by agent  <line no longer in the diff>
      @Leo No Mocha rows are needed for the codes we already know …
      -> you: DOES THIS BREAK EXISTING SIGNALS?
```

How to read it:

| | |
|---|---|
| `[b1f07a24]` | the comment id — pass it to `reply` / `resolve` |
| `R219` / `L48` | anchor: `R` = line 219 of the new side, `L` = old side |
| `by …` | who wrote it (see the table below) |
| `-> author: …` | replies already on the thread; don't repeat what's been said |
| `<FLAGGED-FOR-YOU>` | the human explicitly asked an agent to answer this one — highest priority |
| `<line no longer in the diff>` | the code moved or was reverted since the comment; re-read the file before answering |
| `<resolved>` | already dealt with (only shown with `--all`) |

Useful variants:

```bash
python3 review.py todo --flagged        # only the threads flagged for you
python3 review.py todo -c 8             # include 8 lines of diff context per comment
python3 review.py todo --all            # include resolved threads
python3 review.py pending -c 8          # the flagged queue as JSON, if you'd rather parse
```

## 2. Know who wrote what

| `author` | who it is | what to do |
|---|---|---|
| `you` | the human reviewer | their questions are what you must answer |
| `agent` | you, in earlier turns | don't re-answer; continue or correct it |
| `ai-review-agent` | a separate automated code reviewer | its findings are *suggestions*, not requests from the human — fix them, or reply saying why you disagree |

Anything else is another bot; treat it like `ai-review-agent`.

## 3. Do the work, then write back

Fix the code first. Then:

```bash
python3 review.py reply <file> <comment-id> "what you actually did"
python3 review.py resolve <file> <comment-id>          # only when it's genuinely done
```

Other forms:

```bash
python3 review.py reply --batch -                      # JSON [{path,id,text}, …] on stdin
python3 review.py reply <file> <id> "…" --author ai-review-agent
python3 review.py resolve <file> <id> --reopen         # undo a resolve
```

Replies appear in the human's open browser tab within a couple of seconds. They can
reply back in the UI, and the new reply shows up in your next `todo`.

## Rules

- **Resolve only what is done.** If a thread needs a decision from the human, reply and
  leave it open. Resolving is a claim that the work is finished.
- **Separate verified from assumed.** Say what you ran and what you inferred. Never
  imply tests passed if you didn't run them — say plainly that you didn't.
- **Follow the repo's own rules.** If it has a `CLAUDE.md` / `AGENTS.md` with build,
  test or lint instructions, those win over anything here.
- **Answer the comment that was made.** Don't broaden the scope because you noticed
  something else; leave a new comment for that instead, or mention it in the reply.
- **Re-run `todo` when you finish.** Anything still listed is still yours.

## Prompt to paste

```
You are working through a local code review of this repo. The human's comments live in
.review/state.json and are driven by one script: /path/to/review.py

Start with:
    python3 /path/to/review.py todo

That prints every open thread: [comment-id] anchor, author, the comment text, and any
replies. Add -c 8 for diff context, or --flagged for only the threads flagged for you.

Authors you will see:
  you              the human reviewer — their questions are what you must answer
  agent            you, in earlier turns
  ai-review-agent  a separate automated reviewer; its findings are suggestions, not
                   requests from the human — fix them or say why you disagree

For each thread, do the actual work in the code first, then:
    python3 /path/to/review.py reply <file> <comment-id> "what you did, concretely"
    python3 /path/to/review.py resolve <file> <comment-id>

Rules:
- Only resolve a thread when the work is really done. If it needs the human, reply and
  leave it open.
- Say what you verified versus what you assumed. Don't claim tests passed unless you
  ran them; if the repo's CLAUDE.md says CI runs them, say so instead.
- Don't edit .review/state.json by hand — always go through the script.
- Re-run `todo` at the end; anything still listed is still your problem.
```
