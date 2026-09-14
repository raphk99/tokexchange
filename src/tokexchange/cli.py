"""Command-line interface: ``tokexchange <command>``."""
from __future__ import annotations

import argparse
import json
import logging
import socket
import sys
from pathlib import Path

from . import __version__
from .config import Config, state_dir
from .crypto import SealError, sealer_for
from .models import AgentSpec, Limits
from .transport import TransportError, open_transport


def _transport(cfg: Config, url: str | None = None):
    url = url or cfg.coordinator_url
    if not url:
        raise SystemExit("no coordinator url: set --coordinator, TOKEXCHANGE_URL, or run `tokexchange configure`")
    return open_transport(url, cfg.token), url


def _print_record(rec, verbose: bool = False) -> None:
    line = f"{rec.task_id}  {rec.status:<10} attempts={rec.attempts} worker={rec.worker_id or '-'}  {rec.meta.get('title', '')}"
    print(line)
    if rec.message:
        print(f"    {rec.message}")
    if verbose and rec.result_meta:
        print("    result:", json.dumps(rec.result_meta))


# ----------------------------------------------------------------------------- commands

def cmd_configure(args, cfg: Config) -> int:
    if args.coordinator:
        cfg.coordinator_url = args.coordinator
    if args.token:
        cfg.token = args.token
    if args.secret is not None:
        cfg.secret = args.secret or None
    if args.worker_id:
        cfg.worker["id"] = args.worker_id
    if args.work_root:
        cfg.worker["work_root"] = args.work_root
    if args.pushed_only is not None:
        cfg.submit["pushed_only"] = args.pushed_only
    cfg.save()
    print(f"saved {cfg.path}")
    return 0


def cmd_submit(args, cfg: Config) -> int:
    from .submit import SubmitOptions, build_task, record_submission, submit_task
    request = args.request
    if request == "-":
        request = sys.stdin.read()
    elif args.request_file:
        request = Path(args.request_file).read_text()
    if not request or not request.strip():
        raise SystemExit("empty request")
    sdef = cfg.submit
    agent = AgentSpec(kind=args.agent or sdef.get("agent", "claude-code"), model=args.model or sdef.get("model"),
                      effort=args.effort or sdef.get("effort"), max_turns=args.max_turns or int(sdef.get("max_turns", 60)),
                      timeout_seconds=args.timeout or int(sdef.get("timeout_seconds", 2400)),
                      permission_mode=args.permission_mode or sdef.get("permission_mode", "acceptEdits"),
                      allowed_tools=list(args.allow or []) + list(sdef.get("allowed_tools", [])),
                      disallowed_tools=list(args.disallow or []),
                      context_strategy="resume-transcript" if args.resume_transcript else "brief")
    opts = SubmitOptions(request=request, title=args.title, tests=args.test or [], criteria=args.criteria or [],
                         constraints=args.constraint or [], notes=args.note or [], attach=args.attach or [],
                         session=args.session, include_transcript=bool(args.resume_transcript or args.include_transcript),
                         include_commits=args.include_commits, include_untracked=not args.no_untracked,
                         pushed_only=args.pushed_only or bool(sdef.get("pushed_only", False)),
                         max_context_chars=args.max_context_chars, agent=agent,
                         limits=Limits(max_attempts=args.max_attempts, lease_seconds=args.lease))
    built = build_task(Path.cwd(), opts)
    for w in built.warnings:
        print(f"warning: {w}", file=sys.stderr)
    if args.dry_run:
        out = Path(args.dry_run)
        out.mkdir(parents=True, exist_ok=True)
        (out / f"{built.manifest.task_id}.bundle.tgz").write_bytes(built.bundle)
        (out / "context.md").write_text(built.brief)
        (out / "prompt.md").write_text(built.prompt)
        (out / "task.json").write_text(json.dumps(built.manifest.to_dict(), indent=2))
        print(f"dry run: wrote bundle ({len(built.bundle)} bytes), context.md, prompt.md and task.json to {out}")
        return 0
    transport, url = _transport(cfg, args.coordinator)
    sealer = sealer_for(cfg.secret)
    rec = submit_task(transport, sealer, built)
    from .gitutil import repo_root
    record_submission(built, repo_root(Path.cwd()), url)
    if args.json:
        print(json.dumps(rec.to_dict(), indent=2))
    else:
        print(f"submitted task {rec.task_id} ({len(built.bundle)} bytes, sealed={sealer.name})")
        print(f"  check:  tokexchange status {rec.task_id}")
        print(f"  fetch:  tokexchange fetch {rec.task_id}")
    return 0


def cmd_status(args, cfg: Config) -> int:
    transport, _ = _transport(cfg, args.coordinator)
    rec = transport.get(args.task_id)
    if args.json:
        print(json.dumps(rec.to_dict(), indent=2))
    else:
        _print_record(rec, verbose=True)
    return 0


def cmd_list(args, cfg: Config) -> int:
    transport, _ = _transport(cfg, args.coordinator)
    recs = transport.list(status=args.status, limit=args.limit)
    if args.json:
        print(json.dumps([r.to_dict() for r in recs], indent=2))
    else:
        for r in recs:
            _print_record(r)
        if not recs:
            print("no tasks")
    return 0


def cmd_cancel(args, cfg: Config) -> int:
    transport, _ = _transport(cfg, args.coordinator)
    _print_record(transport.cancel(args.task_id))
    return 0


def cmd_fetch(args, cfg: Config) -> int:
    from .results import fetch_result
    transport, _ = _transport(cfg, args.coordinator)
    out_dir, report = fetch_result(transport, sealer_for(cfg.secret), args.task_id, Path(args.out) if args.out else None)
    print(f"result for {args.task_id}: outcome={report.outcome}")
    if report.error:
        print(f"  error: {report.error}")
    if report.patch:
        p = report.patch
        print(f"  patch: {out_dir / p['file']}  ({p['files_changed']} files, +{p['insertions']} -{p['deletions']})")
        for path in p.get("changed_paths", [])[:50]:
            print(f"    {path}")
    for t in report.tests:
        print(f"  test `{t['command']}` -> exit {t['exit_code']} ({t['duration_seconds']}s)  log: {out_dir / 'logs' / t['log_file']}")
    if report.agent:
        a = report.agent
        print(f"  agent: turns={a.get('num_turns')} session={a.get('session_id')} est_cost_usd={a.get('cost_usd_estimate')}")
        if a.get("summary"):
            print("  agent summary:")
            for line in str(a["summary"]).splitlines()[:40]:
                print(f"    {line}")
    print(f"  files: {out_dir}")
    if report.patch:
        print(f"  apply: tokexchange apply {args.task_id}")
    return 0


def cmd_apply(args, cfg: Config) -> int:
    from .results import apply_result
    result_dir = Path(args.task_id_or_dir)
    if not result_dir.is_dir():
        result_dir = state_dir() / "results" / args.task_id_or_dir
    if not (result_dir / "report.json").is_file():
        raise SystemExit(f"no fetched result at {result_dir}; run `tokexchange fetch <task-id>` first")
    outcome = apply_result(Path.cwd(), result_dir, check_only=args.check, three_way=args.three_way, which=args.which)
    for w in outcome.warnings:
        print(f"warning: {w}", file=sys.stderr)
    if outcome.error:
        print(outcome.error, file=sys.stderr)
        return 1
    if args.check:
        print(f"patch applies cleanly: {outcome.patch_file}")
    else:
        print(f"applied {outcome.patch_file} to the working tree (nothing staged, nothing committed). Review with `git diff`.")
    return 0


def cmd_worker(args, cfg: Config) -> int:
    from .agent import get_agent
    from .worker import Worker, WorkerOptions
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    wcfg = cfg.worker
    transport, _ = _transport(cfg, args.coordinator)
    agent = get_agent(args.agent or wcfg.get("agent", "claude-code"))
    opts = WorkerOptions(worker_id=args.worker_id or wcfg.get("id") or socket.gethostname(),
                         work_root=Path(args.work_root or wcfg.get("work_root") or (state_dir() / "work")),
                         poll_interval=args.poll_interval or float(wcfg.get("poll_interval", 15)),
                         concurrency=args.concurrency or int(wcfg.get("concurrency", 1)),
                         keep_workspaces=args.keep_workspaces or bool(wcfg.get("keep_workspaces", False)))
    worker = Worker(transport, sealer_for(cfg.secret), agent, opts)
    problems = agent.preflight()
    for p in problems:
        print(f"preflight: {p}", file=sys.stderr)
    if problems and not args.once:
        return 2
    if args.once:
        rec = worker.run_once()
        if rec is None:
            print("no queued tasks")
            return 0
        _print_record(rec, verbose=True)
        return 0
    print(f"worker {opts.worker_id} polling {cfg.coordinator_url} every {opts.poll_interval}s (agent={agent.kind}, "
          f"concurrency={opts.concurrency}); Ctrl-C to stop", flush=True)
    try:
        worker.run_forever()
    except KeyboardInterrupt:
        worker.stop()
    return 0


def cmd_coordinator(args, cfg: Config) -> int:
    from .coordinator.server import CoordinatorServer
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    server = CoordinatorServer(Path(args.data_dir), Path(args.tokens), host=args.host, port=args.port,
                               tls_cert=Path(args.tls_cert) if args.tls_cert else None,
                               tls_key=Path(args.tls_key) if args.tls_key else None, verbose=args.verbose)
    print(f"coordinator listening on {server.url} (data: {args.data_dir}, tokens: {args.tokens})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
    return 0


def cmd_token(args, cfg: Config) -> int:
    from .coordinator.server import TokenStore
    token = TokenStore.add_token(Path(args.tokens), args.name, args.roles.split(","))
    print(token)
    return 0


# ----------------------------------------------------------------------------- parser

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tokexchange", description="Delegate Claude Code tasks to another laptop and get back a git patch.")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--config", help="config file (default ~/.config/tokexchange/config.json)")
    sub = p.add_subparsers(dest="command", required=True)

    c = sub.add_parser("configure", help="write coordinator url / token / secret to the config file")
    c.add_argument("--coordinator"); c.add_argument("--token"); c.add_argument("--secret")
    c.add_argument("--worker-id"); c.add_argument("--work-root")
    c.add_argument("--pushed-only", dest="pushed_only", action="store_true", default=None,
                   help="make pushed-only the default for submit on this machine")
    c.add_argument("--allow-uncommitted", dest="pushed_only", action="store_false",
                   help="make shipping uncommitted changes the default again")
    c.set_defaults(func=cmd_configure)

    s = sub.add_parser("submit", help="create a task from the current repository and send it to the coordinator")
    s.add_argument("request", nargs="?", help="the task description ('-' to read stdin)")
    s.add_argument("--request-file", help="read the task description from a file")
    s.add_argument("--title")
    s.add_argument("--test", action="append", help="test command that must pass (repeatable)")
    s.add_argument("--criteria", action="append", help="completion criterion (repeatable)")
    s.add_argument("--constraint", action="append", help="constraint the agent must respect (repeatable)")
    s.add_argument("--note", action="append", help="prior decision / note to include in the brief (repeatable)")
    s.add_argument("--attach", action="append", help="file to ship alongside the task (repeatable)")
    s.add_argument("--session", default="auto", help="Claude Code session id, transcript path, 'auto' or 'none'")
    s.add_argument("--include-transcript", action="store_true", help="ship the raw transcript file too")
    s.add_argument("--resume-transcript", action="store_true", help="EXPERIMENTAL: worker resumes the transcript as history")
    s.add_argument("--include-commits", choices=["auto", "yes", "no"], default="auto")
    s.add_argument("--no-untracked", action="store_true", help="do not ship untracked files")
    s.add_argument("--pushed-only", action="store_true",
                   help="base the task on the pushed HEAD only; warn about (and exclude) uncommitted and untracked work")
    s.add_argument("--max-context-chars", type=int, default=24000)
    s.add_argument("--agent"); s.add_argument("--model"); s.add_argument("--effort")
    s.add_argument("--max-turns", type=int); s.add_argument("--timeout", type=int, help="agent timeout in seconds")
    s.add_argument("--permission-mode", choices=["acceptEdits", "auto", "dontAsk", "bypassPermissions", "default"])
    s.add_argument("--allow", action="append", help="extra --allowedTools rule (repeatable)")
    s.add_argument("--disallow", action="append", help="extra --disallowedTools rule (repeatable)")
    s.add_argument("--max-attempts", type=int, default=2); s.add_argument("--lease", type=int, default=3600)
    s.add_argument("--coordinator"); s.add_argument("--json", action="store_true")
    s.add_argument("--dry-run", metavar="DIR", help="build the bundle and write it plus the brief to DIR without submitting")
    s.set_defaults(func=cmd_submit)

    for name, fn in (("status", cmd_status), ("cancel", cmd_cancel)):
        q = sub.add_parser(name)
        q.add_argument("task_id"); q.add_argument("--coordinator"); q.add_argument("--json", action="store_true")
        q.set_defaults(func=fn)

    l = sub.add_parser("list", help="list tasks on the coordinator")
    l.add_argument("--status"); l.add_argument("--limit", type=int, default=50)
    l.add_argument("--coordinator"); l.add_argument("--json", action="store_true")
    l.set_defaults(func=cmd_list)

    f = sub.add_parser("fetch", help="download and unpack a task result")
    f.add_argument("task_id"); f.add_argument("--out"); f.add_argument("--coordinator")
    f.set_defaults(func=cmd_fetch)

    a = sub.add_parser("apply", help="apply a fetched patch to the current repository's working tree")
    a.add_argument("task_id_or_dir")
    a.add_argument("--check", action="store_true", help="only check whether the patch applies")
    a.add_argument("--3way", dest="three_way", action="store_true")
    a.add_argument("--which", choices=["changes", "full"], default="changes",
                   help="changes = diff vs. your submitted state (default); full = diff vs. base commit")
    a.set_defaults(func=cmd_apply)

    w = sub.add_parser("worker", help="run the worker loop on this machine")
    w.add_argument("--once", action="store_true", help="claim and run at most one task, then exit")
    w.add_argument("--agent", choices=["claude-code", "fake"])
    w.add_argument("--worker-id"); w.add_argument("--work-root"); w.add_argument("--coordinator")
    w.add_argument("--poll-interval", type=float); w.add_argument("--concurrency", type=int)
    w.add_argument("--keep-workspaces", action="store_true")
    w.set_defaults(func=cmd_worker)

    co = sub.add_parser("coordinator", help="run the coordinator service")
    co.add_argument("--data-dir", required=True); co.add_argument("--tokens", required=True)
    co.add_argument("--host", default="127.0.0.1"); co.add_argument("--port", type=int, default=8787)
    co.add_argument("--tls-cert"); co.add_argument("--tls-key"); co.add_argument("--verbose", action="store_true")
    co.set_defaults(func=cmd_coordinator)

    t = sub.add_parser("token", help="mint a device token for the coordinator's token file")
    t.add_argument("--tokens", required=True, help="path to tokens.json (created if missing)")
    t.add_argument("--name", required=True); t.add_argument("--roles", default="submit,work")
    t.set_defaults(func=cmd_token)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = Config.load(Path(args.config) if args.config else None)
    try:
        return args.func(args, cfg)
    except (TransportError, SealError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
