---
name: delegate
description: Delegate the current task to the other laptop through tokexchange and get back a patch. Use when the user says "delegate", "/delegate", "send this to my other laptop", or when local usage is exhausted.
---

# Delegate a task with tokexchange

The user wants this task executed on their other laptop (which has its own
Claude subscription). Your job is to package it well, not to do it.

1. From the conversation so far, write down explicitly:
   - the request (one or two paragraphs, self-contained);
   - decisions already made (each as a `--note`);
   - hard constraints (each as a `--constraint`);
   - completion criteria (each as `--criteria`);
   - test commands that must pass (each as `--test`; use the repo's real commands).
2. Preview the brief and check it is complete:
   ```bash
   tokexchange submit --request-file /tmp/delegate-request.md --note "…" --test "…" --dry-run /tmp/delegate-preview
   cat /tmp/delegate-preview/context.md
   ```
   The transcript of *this* session is picked up automatically.
3. Submit for real (same flags without `--dry-run`) and tell the user the task
   id and the two follow-up commands:
   ```
   tokexchange fetch <id>   and   tokexchange apply <id>
   ```
4. Do not modify the repository yourself. Continue helping the user with
   anything else while the task runs.
