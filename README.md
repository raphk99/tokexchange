# tokexchange

Delegate a Claude Code task from one laptop to another and get back a git
patch. Laptop A (where you work) packages the task and your uncommitted
changes; a small coordinator you host holds the queue; Laptop B (with its own
Claude subscription) runs `claude -p` in an isolated worktree, runs your tests
and uploads a patch plus an execution report; Laptop A downloads and applies it.

Read [`docs/DESIGN.md`](docs/DESIGN.md) first — in particular §0.2 on
Anthropic's credential policy. Short version: each laptop uses **its own**
Claude login, nothing credential-like is ever transferred, and the worker runs
the unmodified `claude` binary.

Requirements: Python ≥ 3.11 and git on all three machines; Claude Code on A
and B (`claude auth status` must report `loggedIn: true` on B). Optional:
`pip install 'tokexchange[crypto]'` on A and B for end-to-end encryption.

```bash
pip install -e .            # in this repo, on each laptop (or pipx install .)
python -m unittest discover -s tests -t .
```

## Runbook

### Coordinator (any host both laptops can reach over HTTPS)

```bash
tokexchange token --tokens /srv/tokexchange/tokens.json --name laptop-a --roles submit   # prints TOKEN_A
tokexchange token --tokens /srv/tokexchange/tokens.json --name laptop-b --roles work     # prints TOKEN_B
tokexchange coordinator --data-dir /srv/tokexchange/data --tokens /srv/tokexchange/tokens.json \
    --host 127.0.0.1 --port 8787
```

Put TLS in front of it. Examples: `examples/Caddyfile` (Caddy reverse proxy
with automatic certificates), `tailscale serve https / http://127.0.0.1:8787`
on a Tailscale node, or `--tls-cert/--tls-key` for a self-managed certificate.
The service is a single Python process with a SQLite file; a $5 VPS or a
free-tier Fly.io machine is plenty.

### Laptop B (personal, runs the work)

```bash
claude                      # log in once with the personal subscription, then exit
tokexchange configure --coordinator https://coord.example.com --token TOKEN_B \
    --secret 'a-long-shared-secret' --worker-id laptop-b --work-root ~/tokexchange-work
tokexchange worker          # polls every 15 s; Ctrl-C to stop
```

Notes:

* The worker calls `claude auth status` at start and refuses to run if B is
  not logged in. Do **not** set `ANTHROPIC_API_KEY` unless you want per-token
  API billing instead of the subscription (it takes precedence).
* If you start the worker over SSH and the macOS Keychain is locked, run
  `claude setup-token` once (in a GUI session) and export
  `CLAUDE_CODE_OAUTH_TOKEN` in the worker's environment.
* `examples/com.tokexchange.worker.plist` runs the worker at login on macOS.
* B needs read access to the git remote (its own SSH key). If B cannot reach
  the remote, submit with `--include-commits yes` so the bundle carries the
  history.

### Laptop A (work, submits and applies)

```bash
tokexchange configure --coordinator https://coord.example.com --token TOKEN_A --secret 'a-long-shared-secret'
cd ~/code/my-repo
tokexchange submit "Implement the CSV export described in our discussion" \
    --test "npm test" --criteria "export button downloads a CSV" \
    --note "we decided to stream the CSV, not buffer it" \
    --constraint "no new dependencies"
```

`submit` captures: branch, HEAD, `git diff HEAD` (your uncommitted work),
untracked non-ignored files, unpushed commits (as a git bundle, when needed),
and a digest of the Claude Code conversation for this directory (the session
you are in when you run it from inside Claude Code, else the most recent).

**Credentials never travel.** Laptop B clones the repo with its own git
access; the bundle only carries a remote URL, a commit id, and files. If you
would rather that no local work leaves Laptop A at all, use
`--pushed-only`: the task is based on the pushed HEAD, and `submit` refuses
to run if HEAD is not on the remote and warns about every modified or
untracked file it is leaving behind. Make it the default with
`tokexchange configure --pushed-only`.

Preview exactly what would be sent:

```bash
tokexchange submit "…" --dry-run /tmp/preview && cat /tmp/preview/context.md
```

Then:

```bash
tokexchange list                       # queue overview
tokexchange status <task-id>
tokexchange fetch  <task-id>           # downloads report + patch, prints summary and test results
tokexchange apply  <task-id> --check   # does it apply cleanly?
tokexchange apply  <task-id>           # applies to the working tree only; review with git diff
```

Nothing is staged or committed for you; your existing uncommitted changes are
untouched. `--3way` and `--which full` help when your tree drifted.

### Delegating from inside Claude Code on Laptop A

Copy `examples/claude-skill/delegate` to `.claude/skills/delegate` in the
repo (or `~/.claude/skills/`). Then `/delegate add pagination to the users
endpoint` makes Claude Code on A assemble the notes/criteria/tests from the
conversation and run `tokexchange submit` for you; it picks up the current
session transcript automatically through `CLAUDE_CODE_SESSION_ID`.

## Useful flags

| Flag | Meaning |
| --- | --- |
| `--session none / <id> / <path.jsonl>` | which transcript to digest (default: auto) |
| `--max-context-chars N` | budget for the conversation excerpt (default 24000) |
| `--attach FILE` | ship a file explicitly (lands in `.tokexchange/attached/` on B) |
| `--include-commits auto/yes/no` | ship unpushed commits (auto: only when needed) |
| `--no-untracked` | do not ship untracked files |
| `--pushed-only` | ship nothing local: base the task on the pushed HEAD, warn about uncommitted/untracked work, refuse if HEAD is unpushed |
| `--permission-mode acceptEdits/auto/dontAsk/bypassPermissions` | worker's Claude Code mode (default acceptEdits) |
| `--allow "Bash(npm *)"` | extra allow-listed tools for the run |
| `--max-turns`, `--timeout`, `--model`, `--effort` | agent limits |
| `--max-attempts`, `--lease` | retry policy on the coordinator |
| `--resume-transcript` | experimental: replay the raw transcript as history on B |

Worker: `--once` (process a single task and exit), `--concurrency N`,
`--keep-workspaces` (keep the worktree for debugging), `--agent fake` (pipeline
test without a model).

## Without a server: shared folder mode

`tokexchange configure --coordinator file:///Users/me/Dropbox/tokexchange` on
both laptops uses a synced folder as the queue. There is no cross-machine
locking, so run a single worker. Good for a first try.

## Layout

```
src/tokexchange/   library + CLI (stdlib only; `cryptography` optional)
tests/             unit, HTTP and end-to-end tests (fake agent); one live test behind TOKEXCHANGE_LIVE=1
docs/DESIGN.md     architecture, research findings, limitations
examples/          Caddyfile, launchd plist, Claude Code skill
```
