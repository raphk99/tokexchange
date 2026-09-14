# Your first delegated job

Assumptions: **Laptop B** is this Mac (personal, Pro login, runs the worker and,
for this first test, the coordinator). **Laptop A** is the work laptop. The two
laptops share no credentials; the only thing both need is read access to one
small public test repo.

## Part 1 — Laptop B, one-time preparation (10 min)

1. Let Laptop A install the tool. The `tokexchange` repo is private; make it
   public (it contains no secrets) or AirDrop a zip of it instead.
   ```bash
   gh repo edit raphk99/tokexchange --visibility public --accept-visibility-change-consequences
   ```
2. Create a tiny public playground repo so neither laptop needs the other's
   GitHub account:
   ```bash
   mkdir -p ~/tokexchange-playground && cd ~/tokexchange-playground && git init -b main
   printf 'def add(a, b):\n    return a + b\n' > calc.py
   printf 'import unittest, calc\n\nclass T(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(calc.add(2, 3), 5)\n' > test_calc.py
   printf '# playground\n' > README.md
   git add -A && git commit -m "playground" && gh repo create tokexchange-playground --public --source=. --push
   ```
3. Mint one token per laptop and keep the two values somewhere handy:
   ```bash
   mkdir -p ~/tokexchange-coord
   tokexchange token --tokens ~/tokexchange-coord/tokens.json --name laptop-a --roles submit   # TOKEN_A
   tokexchange token --tokens ~/tokexchange-coord/tokens.json --name laptop-b --roles work     # TOKEN_B
   ```
4. Choose a shared secret (any long passphrase). Both laptops will use it, and
   both need the `cryptography` package for it; this Mac already has it.

## Part 2 — Laptop B, three terminals that stay open

Terminal 1, the coordinator:
```bash
tokexchange coordinator --data-dir ~/tokexchange-coord/data --tokens ~/tokexchange-coord/tokens.json --port 8787
```

Terminal 2, a public HTTPS address for it (no account needed):
```bash
brew install cloudflared
cloudflared tunnel --url http://127.0.0.1:8787
```
Copy the `https://….trycloudflare.com` URL it prints. It changes every time
you restart the tunnel. (Alternative if both laptops are on one tailnet:
`tailscale serve https / http://127.0.0.1:8787`.)

Terminal 3, the worker (from a normal Terminal window, not over SSH, so the
Keychain login is available):
```bash
claude auth status            # must show "loggedIn": true
tokexchange configure --coordinator http://127.0.0.1:8787 --token TOKEN_B \
    --secret 'YOUR-SHARED-SECRET' --worker-id laptop-b --work-root ~/tokexchange-work
tokexchange worker
```
It prints `worker laptop-b polling … every 15.0s` and then waits.

## Part 3 — Laptop A, one-time preparation (5 min)

```bash
python3 --version             # needs 3.11 or newer
git clone https://github.com/raphk99/tokexchange.git ~/tokexchange
cd ~/tokexchange && python3 -m pip install -e '.[crypto]'
tokexchange --version
tokexchange configure --coordinator https://YOUR-TUNNEL.trycloudflare.com --token TOKEN_A --secret 'YOUR-SHARED-SECRET'
tokexchange list              # should print "no tasks", proving auth and TLS work
git clone https://github.com/raphk99/tokexchange-playground.git ~/playground
```

## Part 4 — Laptop A, the job itself

```bash
cd ~/playground
tokexchange submit "Add mul(a, b) and div(a, b) to calc.py. div must raise ZeroDivisionError with a clear message. Add tests for both." \
    --test "python3 -m unittest" --criteria "all functions have docstrings" \
    --session none --dry-run /tmp/preview
cat /tmp/preview/context.md          # this is exactly what Laptop B's Claude will read
```
Happy with it? Submit for real (same command without `--dry-run`). It prints a
task id. Then:
```bash
tokexchange status <id>              # queued -> claimed -> running (watch Terminal 3 on B too)
tokexchange fetch <id>               # outcome, patch stats, test results, agent summary
tokexchange apply <id> --check
tokexchange apply <id>
git diff                             # review; nothing is staged or committed for you
python3 -m unittest
```
A first job of this size takes roughly one to three minutes on Laptop B.

## Part 5 — Second job, this time with conversation context

On Laptop A, inside the playground, start `claude`, discuss a change for a few
turns (for example, agree that errors should be custom exception classes),
then in the same session type:
```
! tokexchange submit "Implement what we just agreed on" --test "python3 -m unittest" --dry-run /tmp/preview2
```
Open `/tmp/preview2/context.md`: the "Conversation history" section now
contains your discussion, because `submit` finds the transcript of the session
it runs in. Submit without `--dry-run`, then fetch and apply as before.

## If something goes wrong

| Symptom | Fix |
| --- | --- |
| `tokexchange list` on A: cannot reach coordinator | Tunnel URL changed or Terminal 2 closed; re-run `tokexchange configure --coordinator …` on A |
| `cannot reach coordinator … CERTIFICATE_VERIFY_FAILED` | Your Python has no CA bundle: `pip install certifi` (python.org macOS builds: run `Install Certificates.command`); self-signed coordinator cert: set `TOKEXCHANGE_CA_BUNDLE` |
| `401 invalid or missing token` | Token pasted wrong or roles swapped (A needs `submit`, B needs `work`) |
| `decryption failed` / `refusing to accept plaintext` | Secrets differ, or `cryptography` missing on one side (`pip install cryptography`) |
| Worker: `claude is not logged in` | Run `claude` once in a Terminal window on B; or `claude setup-token` and export `CLAUDE_CODE_OAUTH_TOKEN` |
| Result outcome `workspace_error: cannot clone` | B cannot reach the repo's remote; use a public repo, or submit with `--include-commits yes` |
| Result outcome `agent_error` with a usage-limit message | B's 5-hour or weekly cap is reached; wait, then resubmit |
| Claude on B could not run a command | Only `--test` commands and read-only git are allow-listed; add `--allow "Bash(pip *)"` etc., or `--permission-mode auto` |
| `apply`: patch does not apply | Your tree changed since submitting; try `--3way`, or `--which full` on a clean checkout |

Every result keeps the exact `claude` command line and raw logs under
`~/.local/share/tokexchange/results/<id>/logs/` on Laptop A, and the worker's
copy under `~/tokexchange-work/tasks/<id>/` on Laptop B.

## Stopping

Ctrl-C in the three terminals on B. Queue and results stay in
`~/tokexchange-coord/data` until you delete the folder. To keep the worker
running permanently, see `examples/com.tokexchange.worker.plist`.
