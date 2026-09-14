# tokexchange — technical design

Delegate a Claude Code task from Laptop A (work) to Laptop B (personal) through a
remote coordinator, and get back a git patch against a known base commit.

This document records the design decisions and the research behind them. The
prototype in this repository implements everything marked **[prototype]**;
items marked **[later]** are designed but not built.

---

## 0. Research findings that shape the design

All findings were verified on 2026-09-14 against Claude Code **2.1.268** on
macOS and the official documentation at code.claude.com.

### 0.1 Authentication

| Fact | Source |
| --- | --- |
| `claude -p` (non-interactive "print" mode) uses the machine's existing credential. Precedence: cloud-provider vars → `ANTHROPIC_AUTH_TOKEN` → `ANTHROPIC_API_KEY` → `apiKeyHelper` → `CLAUDE_CODE_OAUTH_TOKEN` → Anthropic profiles → **subscription OAuth login from `/login`** ("the default for Claude Pro, Max, Team, and Enterprise users"). | docs: Authentication → Authentication precedence |
| A Pro/Max login therefore works for scripted `claude -p` runs on the machine that performed the login. Verified: a `claude -p … --output-format json` run on this machine (`auth status` = `claude.ai`, `subscriptionType: pro`) succeeded without any API key. | local smoke test |
| `claude setup-token` mints a one-year OAuth token for "CI pipelines, scripts, or other environments where interactive browser login isn't available"; it "requires a Pro, Max, Team, or Enterprise plan" and is consumed via `CLAUDE_CODE_OAUTH_TOKEN`. | docs: Authentication → Generate a long-lived token |
| `--bare` mode "never reads OAuth credentials or the system keychain" and needs `ANTHROPIC_API_KEY`. So the worker must **not** use `--bare`. | docs: Headless → bare mode |
| On macOS credentials live in the Keychain; when the Keychain is locked (SSH session, headless login) Claude Code falls back to `~/.claude/.credentials.json`. A worker started from a GUI login session works; a worker started over SSH may need `CLAUDE_CODE_OAUTH_TOKEN`. | docs: Authentication → Credential management |
| A subscription **cannot** be used through the Messages API. The Agent SDK and any custom API client need a Console API key, billed per token. | docs: Legal and compliance → Authentication and credential use |

### 0.2 Policy constraints (read this before deploying)

Anthropic's usage policy (docs → Legal and compliance) says OAuth credentials
from Free/Pro/Max/Team/Enterprise plans are "intended exclusively for
purchasers of … subscription plans and … designed to support ordinary use of
Claude Code and other native Anthropic applications", and that developers
"may not collect, store, or intermediate Claude.ai credentials or session
tokens". It also notes that "advertised usage limits for Pro and Max plans
assume ordinary, individual usage of Claude Code and the Agent SDK".

Design consequences:

1. **Each laptop authenticates itself.** Laptop B runs the unmodified `claude`
   binary with Laptop B's own login. No credential of any kind is ever placed
   in a task bundle, in the coordinator, or in the config file of the other
   laptop. The system only moves *tasks and patches*.
2. **This is a one-person, two-device workflow**, not a service for other
   users. Whether a queue-fed worker still counts as "ordinary individual
   usage" is a judgement call about your own subscription; the design keeps
   the worker as close as possible to "the same developer running `claude -p`
   on their own laptop", but you should read the policy yourself.
3. **If you want a fully supported unattended path**, use an API key from the
   Claude Console on Laptop B (`ANTHROPIC_API_KEY`), which is explicitly
   intended for automation and is billed per token. The worker supports it
   unchanged: it simply inherits the environment.

### 0.3 Non-interactive execution facts used by the worker

* `claude -p "<prompt>" --output-format json` prints one JSON object with
  `result`, `session_id`, `is_error`, `subtype`, `num_turns`,
  `total_cost_usd` (a client-side estimate), `permission_denials`,
  `terminal_reason`. Exit code is non-zero on failure. Verified live.
* Print mode starts in **Manual** permission mode on every plan, so the worker
  must pass `--permission-mode` explicitly. `--permission-prompts none`
  (v2.1.259+) makes anything that would prompt be denied instead of hanging.
* `--max-turns`, `--append-system-prompt-file`, `--allowedTools`,
  `--disallowedTools`, `--model`, `--effort`, `--name` all work with `-p`.
  Verified live.
* Print mode skips the workspace-trust dialog and **does** run hooks and MCP
  servers found in the project (`.claude/settings.json`, `.mcp.json`) — this is
  a supply-chain consideration for the worker (see §11).
* `--resume <absolute path to .jsonl>` can resume a transcript file from any
  project; transcripts live at `~/.claude/projects/<cwd with non-alphanumerics
  → '-'>/<session-id>.jsonl`. The JSONL format is documented as *internal and
  subject to change*.
* A shell command run from inside a Claude Code session sees
  `CLAUDE_CODE_SESSION_ID` in its environment, which lets the submitter find
  the transcript of the session it was launched from.

---

## 1. Overall architecture

```
 Laptop A (work)                   Coordinator (VPS / Fly / Tailscale node)        Laptop B (personal)
 ─────────────────                 ────────────────────────────────────────        ───────────────────
 tokexchange submit  ──POST bundle──▶  SQLite queue + blob store  ◀──claim/poll──  tokexchange worker
   • git metadata                       • bearer-token auth, roles                     • git mirror + worktree
   • uncommitted patch                  • leases, attempts, status                     • apply baseline
   • transcript digest                  • stores only opaque blobs                     • claude -p …  (B's login)
   • brief + prompt                                                                    • run tests
 tokexchange fetch   ◀──GET result──   result blob  ◀──────PUT result───────────       • git diff → patch
 tokexchange apply                                                                     • report.json
```

Module map (`src/tokexchange/`):

| Module | Role | Replaceable? |
| --- | --- | --- |
| `models.py` | versioned JSON documents: task manifest, execution report | schema_version |
| `context.py` | transcript digest → Markdown brief; prompt rendering | context strategy |
| `gitutil.py` | git operations (never mutates A's tree/index) | |
| `bundle.py` | tar.gz pack/unpack with path-traversal protection | |
| `crypto.py` | optional end-to-end AES-256-GCM sealing | Sealer interface |
| `transport/` | `Transport` ABC; `LocalDirTransport` (file://), `HttpTransport` (http[s]://) | **yes**: add S3/Git/… |
| `coordinator/server.py` | stdlib HTTP service + SQLite queue | **yes**: any server implementing the REST surface |
| `agent/` | `Agent` ABC; `ClaudeCodeAgent`, `FakeAgent` | **yes**: Codex, Aider, … |
| `submit.py` / `results.py` | Laptop A logic | |
| `worker.py` | Laptop B logic | |
| `cli.py` | `tokexchange …` | |

### Alternatives considered

* **Git remote as the coordinator** (push `tasks/<id>` branches, worker polls
  the remote). Zero new infrastructure and auth already exists, but task
  metadata/status would be encoded in refs, results pollute the remote, and
  Laptop A's work remote may be a corporate GitHub you should not push
  personal-laptop output to. Kept as a possible `Transport` **[later]**.
* **Claude Code Remote Control / Claude Code on the web.** Remote Control
  drives a session on another device but from the *same* account, and cloud
  sessions bill the account that launched them. Neither lets Laptop B's
  subscription execute Laptop A's task.
* **Direct connection (Tailscale + ssh).** Works only when B is reachable; a
  queue decouples the laptops in time, which matches "B may be asleep".
* **Agent SDK instead of the CLI.** Would give richer control but requires an
  API key per the policy, and the CLI already exposes everything needed.

---

## 2. Task format

A task is a gzip tarball ("bundle"):

```
task.json            manifest (see below)
prompt.md            the user-turn prompt handed to the agent
context.md           the brief appended to the agent's system prompt
uncommitted.patch    `git diff --binary HEAD` on Laptop A (optional)
files/untracked/…    untracked, non-ignored files from Laptop A (optional)
files/attached/…     files the user attached explicitly (optional)
commits.bundle       `git bundle` of commits missing from the remote (optional)
transcript.jsonl     raw Claude Code transcript (optional, experimental)
```

`task.json` (schema_version 1):

```json
{
  "schema_version": 1, "task_id": "uuid", "title": "…", "request": "verbatim user request",
  "repo": {"remote_url": "git@…", "branch": "feature/x", "base_commit": "sha",
           "has_uncommitted_patch": true, "uncommitted_patch_sha256": "…",
           "untracked_files": ["notes.txt"], "has_commit_bundle": false,
           "commit_bundle_ref": "refs/tokexchange/base"},
  "acceptance": {"tests": ["npm test"], "criteria": ["…"], "timeout_seconds": 900},
  "constraints": ["no new dependencies"],
  "context": {"brief_file": "context.md", "transcript_file": null,
              "attached_files": [], "source_session_id": "…"},
  "agent": {"kind": "claude-code", "model": null, "effort": null, "max_turns": 60,
            "timeout_seconds": 2400, "permission_mode": "acceptEdits",
            "allowed_tools": [], "disallowed_tools": [], "context_strategy": "brief"},
  "limits": {"max_attempts": 2, "lease_seconds": 3600},
  "created_at": "…", "created_by": "hostname"
}
```

The coordinator only ever sees `TaskManifest.summary_meta()` (id, title,
branch, base commit, lease/attempt limits) in clear; the bundle itself may be
encrypted.

The result is another tarball:

```
report.json          ExecutionReport (outcome, agent stats, tests, patch stats, error)
changes.patch        diff vs. the *baseline* (= base commit + A's uncommitted work)
full.patch           diff vs. the base commit (only when a baseline commit was made)
logs/agent_*.{json,log,txt}   claude stdout JSON, stderr, exact command line
logs/test_<n>.log
```

Outcomes: `success`, `tests_failed`, `no_changes`, `agent_error`, `timeout`,
`workspace_error`.

---

## 3. Context collection and reconstruction

**The whole conversation cannot simply be transferred.** Reasons found:

* The transcript format is internal and version-dependent; tool results embed
  Laptop A paths and outputs; a long session can be hundreds of KB, most of it
  tool output that is irrelevant on B and costly to replay.
* `--resume` of a foreign transcript is documented to work by file path, but it
  restores *everything* (including the model and the permission mode), and on
  Pro/Max a >100k-token session triggers "resume from summary" behaviour.
* The "context" the user cares about is mostly *decisions and requirements*,
  which are better stated explicitly than mined.

Strategy **[prototype]** — a layered, structured brief:

1. **The request, verbatim** (`prompt.md`), plus operating rules (no push, no
   questions, run these tests, finish with a summary).
2. **Repository facts**: branch, base commit, whether a baseline of
   uncommitted work was applied.
3. **Explicit knowledge** supplied on the command line: `--note` (prior
   decisions), `--constraint`, `--criteria`, `--test`, `--attach FILE`.
4. **Conversation digest** extracted from the Claude Code transcript of the
   current session (found via `CLAUDE_CODE_SESSION_ID`, `--session <id>`, or
   the most recent transcript for the directory): human prompts and assistant
   prose only; tool calls, tool results, thinking, sidechains (subagents),
   meta messages and `<system-reminder>` blocks are dropped. The first user
   message is always kept; recent turns fill a character budget (default 24k).
   Compaction summaries are kept as "Earlier conversation summary".
5. **Repository-resident context** travels for free: `CLAUDE.md`, `.claude/`
   rules and skills are in the repo, and the worker deliberately does not use
   `--bare`, so Claude Code on B loads them as it would for the developer.

The brief is delivered with `--append-system-prompt-file`, so the default
Claude Code system prompt (tools, safety, CLAUDE.md loading) stays intact.

Strategy **[experimental, wired but not validated]** — `--resume-transcript`:
ship the raw `.jsonl` and start the worker's run with
`--resume <file> --fork-session`. Useful when a very long design discussion
matters more than token cost. It depends on an internal format and needs
testing on a real long session before relying on it.

**Preview before sending:** `tokexchange submit … --dry-run DIR` writes the
bundle, `context.md` and `prompt.md` locally so the brief can be inspected and
edited into `--note`s if it missed something.

---

## 4. Repository synchronisation

* Laptop A records `remote_url`, `branch`, `base_commit = HEAD`.
* **Uncommitted tracked changes** are captured with `git diff --binary HEAD`
  (staged and unstaged together). **Untracked, non-ignored files** are copied
  into the bundle (limits: 500 files, 5 MiB each). Laptop A's index and
  working tree are never modified.
* **Unpushed commits**: if `HEAD` is not reachable from any remote-tracking
  ref, a `git bundle` containing the missing commits is created (via a
  temporary ref `refs/tokexchange/base`) and shipped. The worker fetches it
  into its mirror. `--include-commits yes|no|auto` overrides.
* Laptop B keeps one **bare mirror per remote URL** under
  `<work_root>/mirrors/`, fetched on each task under a per-mirror lock, and
  creates a **detached worktree per task** at `base_commit`. Repository hooks
  are disabled in the worktree (`core.hooksPath=/dev/null`).
* B applies the uncommitted patch and untracked files and commits them as the
  **baseline commit** ("tokexchange: submitter's uncommitted baseline"). The
  returned `changes.patch` is `git diff baseline` after `git add -A`, so it
  contains only the agent's work whether the agent committed or not, and
  applies directly onto Laptop A's current working tree. `full.patch` (vs.
  `base_commit`) is included for review or for applying on a clean checkout.
* Laptop A's `apply` checks: HEAD equals the base commit; the current
  uncommitted diff still hashes to what was submitted (warns otherwise);
  `git apply --check`; then applies to the working tree only (no `--index`),
  so staging state and unrelated uncommitted edits are untouched. `--3way`
  and `--which full` are available for drift.

Auth to the git remote on B is B's own (SSH key / credential helper); no git
credential of A is ever transported. For a corporate remote B cannot reach,
`--include-commits yes` plus a local-only mirror works: the bundle then
carries the full history needed **[prototype]**.

**Pushed-only mode [prototype]** (`--pushed-only`, or `submit.pushed_only` in
config): the task is based on the pushed HEAD and nothing local is shipped.
`submit` refuses when HEAD is not on the remote and warns, naming the files,
when modified or untracked work is being left out. The worker then makes no
baseline commit, `changes.patch` equals the diff against `base_commit`, and
applying it onto A works whether or not A still has uncommitted edits (as long
as they do not overlap). This is the recommended mode when you want the
minimum data leaving Laptop A.

---

## 5. Secure communication between laptops

* Neither laptop accepts inbound connections. Both make outbound HTTPS calls
  to the coordinator; B long-polls (`claim` every N seconds).
* Transport security is TLS: either the coordinator's built-in
  `--tls-cert/--tls-key`, or (recommended) a reverse proxy / platform that
  terminates TLS (Caddy, Fly.io, a Tailscale node with `tailscale serve`).
* **End-to-end sealing [prototype, optional]**: a shared secret configured on
  both laptops derives an AES-256-GCM key (PBKDF2-HMAC-SHA256, 200k rounds,
  random salt and nonce per message). Bundles and results are ciphertext to
  the coordinator; it stores only `summary_meta` in clear. A sealer configured
  with a secret refuses plaintext payloads, and a `NullSealer` refuses
  ciphertext, so misconfiguration fails loudly. Requires `cryptography`.
* Payload integrity: GCM tag when sealed; `sha256` of the patch in the report.

---

## 6. Authentication and authorization

* **Coordinator**: bearer tokens minted with `tokexchange token --name laptop-a
  --roles submit` (stored hashed? — no: stored in `tokens.json` with mode
  0600; compared with `hmac.compare_digest`). Roles: `submit` (create, read,
  cancel, download result) and `work` (claim, download bundle, heartbeat,
  upload result). A device can hold both. **[later]**: hash tokens at rest,
  per-token task ownership, expiry, revocation list.
* **Anthropic**: each laptop's own `claude` login; see §0.1/0.2. Nothing is
  shared. The worker's preflight runs `claude auth status` and refuses to start
  when not logged in.
* **Git remote**: each laptop's own credentials.

---

## 7. Claude Code execution on Laptop B

Command built by `ClaudeCodeAgent.build_command` (exact, see
`logs/agent_command.txt` in every result):

```
claude -p "<prompt.md>" --output-format json --permission-prompts none
       --max-turns <N> --append-system-prompt-file <context.md> --name tokexchange-<id>
       --permission-mode acceptEdits            # or auto / dontAsk / --dangerously-skip-permissions
       [--model …] [--effort …]
       --allowedTools "Bash(git status *),Bash(git diff *),…,Bash(<each test cmd>),Bash(<each test cmd> *),<user extras>"
       --disallowedTools "Bash(git push *),Bash(git remote *),Bash(git fetch *),Bash(git pull *),Bash(gh pr *),Bash(gh repo *)"
```

* Runs in the task worktree with stdin closed and its own process group; the
  environment variables of an enclosing Claude Code session are stripped.
* **Timeout**: SIGINT (ends the turn cleanly) → SIGTERM → SIGKILL.
* **Cancel**: the heartbeat thread notices `cancelled` and the agent is
  interrupted the same way.
* **Permission mode trade-off**: `acceptEdits` is deterministic (edits and
  file-system commands allowed; other shell commands only when allow-listed,
  which the worker does for declared test commands). `auto` delegates to the
  classifier and is closer to interactive behaviour but can be disabled by
  org settings. `bypassPermissions` is only sensible inside a VM/container and
  is refused by Claude Code when running as root.
* The result JSON is parsed for `is_error`, `num_turns`, `session_id`,
  `total_cost_usd` (estimate; a subscription is not billed per token) and
  `permission_denials`; the `result` text becomes the agent summary shown by
  `tokexchange fetch`.
* Session persistence is left on so the run can be inspected on B with
  `claude --resume <session_id>`.

Usage-limit behaviour: when B's 5-hour or weekly cap is hit, Claude Code
returns an error result; the task ends as `agent_error` with the message in
the report. **[later]**: detect `rate_limit` in stream-json events and
requeue with a delay instead of consuming an attempt.

---

## 8. Patch generation and transfer

Covered in §4: `changes.patch` (vs. baseline) and `full.patch` (vs. base) are
produced with `git add -A && git diff --cached --binary`, so renames, binary
files and new files are included. Stats and changed paths go into
`report.json`. The result bundle is sealed and `PUT` to the coordinator; A
fetches it into `~/.local/share/tokexchange/results/<id>/` and applies it.

---

## 9. Failure handling

| Failure | Behaviour |
| --- | --- |
| Worker crashes / loses network mid-task | Lease expires (default 1 h, refreshed by heartbeats every 60 s); task returns to `queued` while `attempts < max_attempts`, then `failed`. |
| Remote unreachable from B | `workspace_error` with the git message; if a commit bundle is present the worker proceeds without the remote. |
| Base commit not fetchable | `workspace_error: base commit … not reachable; push it or resubmit with --include-commits yes`. |
| Agent produced no JSON / non-zero exit | `agent_error`; stderr tail in the report; any partial patch is still returned. |
| Agent timeout | `timeout`; partial patch returned. |
| Tests fail | `tests_failed`; patch and logs returned so A can decide. |
| Cancelled by A | Worker interrupts the agent and uploads nothing. |
| A's tree changed since submission | `apply` warns (hash mismatch) and runs `git apply --check` first; `--3way` available. |
| Wrong/missing shared secret | Decrypt fails loudly on either side; nothing is silently accepted in clear. |

Every result includes the exact agent command line and raw logs.

---

## 10. Concurrent tasks

* Coordinator: claims run inside a SQLite `BEGIN IMMEDIATE` transaction, so
  concurrent workers never receive the same task (tested with 4 threads × 6
  tasks). FIFO by creation time.
* Worker: `--concurrency N` runs N tasks in a thread pool. Each task has its
  own worktree; the shared mirror is fetched under a lock. Heartbeats are
  per task.
* Submitter: any number of outstanding tasks; each is independent and applies
  independently. Two patches touching the same lines will conflict at apply
  time, as with any parallel work.
* **[later]**: priorities, per-repo serialization option, dependency between
  tasks ("apply task X's patch first").

---

## 11. Security and privacy

* **What leaves Laptop A**: the request, the brief (including a digest of
  your conversation), your uncommitted diff, untracked files, possibly
  unpushed commits, and attachments. Use `--dry-run` to inspect. Untracked
  files respect `.gitignore`, so ignored secrets (`.env`) are not shipped, but
  review anyway; `--no-untracked` and `--session none` reduce exposure.
* **Coordinator trust**: with a shared secret the coordinator sees only
  ciphertext plus title/branch/commit ids. Without it, the coordinator sees
  code and conversation excerpts; run it on infrastructure you control.
* **Prompt-injection / supply chain on B**: the agent executes with the
  permissions in §7 inside a worktree of a repository you control. Because
  `-p` loads project hooks and `.mcp.json`, only delegate repositories you
  trust. Repository git hooks are disabled in the worktree. Consider running
  the worker inside a VM or container for defence in depth.
* **Credentials**: never transported. `ANTHROPIC_API_KEY` in B's environment
  would silently take precedence over the login (documented precedence); the
  worker does not set or unset it.
* **Bundle safety**: extraction rejects absolute paths, `..`, symlinks and
  hard links. Task ids are validated server-side.
* **Data retention**: coordinator blobs persist until deleted; **[later]** add
  a retention sweep and a `purge` command.

---

## 12. Testing

* `python -m unittest discover -s tests -t .` — 27 fast tests, no network,
  no model calls:
  * models round-trip / schema guard; bundle traversal protection; crypto
    round-trip and misconfiguration failures;
  * transcript parsing (tool results, sidechains, meta and system reminders
    dropped; budgeting keeps first + recent turns; session lookup via env);
  * `LocalDirTransport` lifecycle, lease expiry, cancel;
  * coordinator over real HTTP: auth/roles, lifecycle, cancel semantics,
    concurrent claims, lease expiry;
  * end-to-end with `FakeAgent`: temp origin + clone with uncommitted and
    untracked changes → submit (sealed) → worker → fetch → apply; unpushed
    commits via bundle; failing tests still return a patch; `no_changes`;
    unreachable base commit; transcript digest in brief; CLI dry run.
* `TOKEXCHANGE_LIVE=1 python -m unittest tests.test_live_claude` — runs the
  real `claude` on a tiny repo through the whole pipeline using this
  machine's login (costs a little quota).
* Manual two-laptop test: README "Runbook".

**[later]**: property tests for patch/apply round-trips, a fake `claude`
binary for exercising timeout/cancel paths, load tests for the coordinator.

---

## 13. Known limitations / next steps

1. Transcript digest is heuristic and format-dependent; `--resume-transcript`
   is unvalidated.
2. No streaming progress from B to A (only coarse status messages via
   heartbeat). `stream-json` could feed a progress endpoint.
3. Tokens stored in clear on the coordinator; no expiry/rotation UI.
4. No retention/cleanup of coordinator blobs or worker mirrors.
5. Rate-limit-aware requeue on B (§7).
6. Worker as a launchd/systemd service is documented but not packaged.
7. A Claude Code skill (`examples/claude-skill/delegate`) lets you type
   `/delegate …` on Laptop A; it shells out to `tokexchange submit` and needs
   the CLI on PATH.
