"""Command-line interface (stage 4).

Commands::

    fsu scan              read-only report (optionally with --ai advice)
    fsu quarantine        plan (dry run) or, with --apply, move items to quarantine
    fsu list-quarantine   items currently in quarantine
    fsu restore           move items back
    fsu purge             permanently delete quarantined items

Rules: FSU never runs as root; nothing changes without ``--apply`` (or an
explicit ``restore``/``purge``); anything that changes files asks for
confirmation unless ``--yes`` is given; interrupted operations are recovered
before any change.

Exit codes: 0 success, 1 error, 2 usage error, 3 some items were blocked.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from fsu import __version__
from fsu.ai_engine import DEFAULT_URL, AiEngine, AiReview, OllamaClient
from fsu.collector import Collector
from fsu.models import Action, Candidate, ScanReport
from fsu.quarantine import BatchResult, QuarantineError, QuarantineStore, execute
from fsu.safeguard import RootRefusedError, SnapshotProvider, ensure_not_root
from fsu.system import Runner, run_command

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_BLOCKED = 3

DAY = 86400.0


@dataclass
class Context:
    """Everything the CLI touches, injectable for tests."""

    home: Path = field(default_factory=Path.home)
    runner: Runner = run_command
    desktop_dirs: Iterable[Path] | None = None
    stdout: TextIO = field(default_factory=lambda: sys.stdout)
    stderr: TextIO = field(default_factory=lambda: sys.stderr)
    stdin: TextIO = field(default_factory=lambda: sys.stdin)
    clock: Callable[[], float] = time.time

    def out(self, text: str = "") -> None:
        print(text, file=self.stdout)

    def err(self, text: str) -> None:
        print(text, file=self.stderr)


class UsageError(Exception):
    """Invalid combination of arguments."""


# -- formatting -----------------------------------------------------------------------


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GiB"  # pragma: no cover


def _print_candidates(
    ctx: Context, candidates: Sequence[Candidate], review: AiReview | None
) -> None:
    for c in candidates:
        ctx.out(f"{c.score:.2f}  {c.action.value:<25} {human_size(c.size_bytes):>10}  {c.path}")
        ctx.out("      └─ " + "; ".join(e.detail for e in c.evidence))
        opinion = review.opinions.get(c.path) if review else None
        if opinion is not None:
            flag = " [demoted]" if opinion.demoted else ""
            ctx.out(
                f"      AI: {opinion.verdict.value} ({opinion.confidence:.2f}){flag}"
                f" - {opinion.rationale}"
            )


def _confirm(ctx: Context, question: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not ctx.stdin.isatty():
        raise UsageError("not running interactively; pass --yes to confirm")
    ctx.stdout.write(f"{question} [y/N] ")
    ctx.stdout.flush()
    return ctx.stdin.readline().strip().lower() in ("y", "yes")


# -- shared steps ---------------------------------------------------------------------


def _scan(ctx: Context, args: argparse.Namespace) -> tuple[ScanReport, AiReview | None]:
    report = Collector(home=ctx.home, runner=ctx.runner, desktop_dirs=ctx.desktop_dirs).collect()
    review = None
    if getattr(args, "ai", False):
        engine = AiEngine(OllamaClient(args.ollama_url), args.model, clock=ctx.clock)
        review = engine.review(report.candidates)
    return report, review


def _warnings(ctx: Context, report: ScanReport, review: AiReview | None) -> None:
    for warning in [*report.warnings, *(review.warnings if review else [])]:
        ctx.err(f"warning: {warning}")


def _store(ctx: Context) -> QuarantineStore:
    store = QuarantineStore(ctx.home, clock=ctx.clock)
    for message in store.recover():
        ctx.err(f"recovered: {message}")
    return store


# -- commands -------------------------------------------------------------------------


def cmd_scan(ctx: Context, args: argparse.Namespace) -> int:
    report, review = _scan(ctx, args)
    candidates = review.candidates if review else report.candidates
    if args.json:
        data = report.to_dict()
        data["candidates"] = [c.to_dict() for c in candidates]
        if review is not None:
            data["ai"] = {
                "model": review.model,
                "available": review.available,
                "seconds": round(review.seconds, 1),
                "opinions": [o.to_dict() for o in review.opinions.values()],
                "warnings": review.warnings,
            }
        json.dump(data, ctx.stdout, ensure_ascii=False, indent=2)
        ctx.out()
        return EXIT_OK

    _print_candidates(ctx, candidates, review)
    total = sum(c.size_bytes for c in candidates)
    ctx.out(
        f"\n{len(candidates)} candidates ({human_size(total)}); "
        f"{len(report.protected_skipped)} protected; "
        f"{report.installed_skipped} match something installed; "
        f"{len(report.regenerated_skipped)} recreated by other software."
    )
    if report.low_confidence_hidden:
        ctx.out(
            f"{report.low_confidence_hidden} low-confidence item(s) in $HOME, ~/.local/state "
            "and launcher folders are not listed (only >= 0.70 is shown there)."
        )
    if review is not None and review.available:
        ctx.out(f"AI advice: {review.model}, {review.seconds:.1f}s (advice only, never promotes)")
    ctx.out("Nothing was changed. Use 'fsu quarantine' to plan a clean-up.")
    _warnings(ctx, report, review)
    if args.timings:
        for phase, seconds in report.timings.items():
            ctx.err(f"timing: {phase:<15} {seconds:7.2f}s")
    return EXIT_OK


def _print_plan(ctx: Context, result: BatchResult, title: str) -> None:
    ctx.out(title)
    for o in result.outcomes:
        reasons = "; ".join(f"{b.code}: {b.detail}" for b in o.blocks)
        suffix = f" ({o.entry_id})" if o.entry_id else ""
        ctx.out(f"  {o.status:<17} {o.path}{suffix}" + (f"\n      └─ {reasons}" if reasons else ""))


def cmd_quarantine(ctx: Context, args: argparse.Namespace) -> int:
    ensure_not_root()
    store = _store(ctx)
    report, review = _scan(ctx, args)
    candidates = list(review.candidates if review else report.candidates)
    by_path = {c.path: c for c in candidates}

    selected = [Path(p).expanduser().absolute() for p in args.select]
    unknown = [p for p in selected if p not in by_path]
    if unknown:
        raise UsageError("--select must name scan candidates: " + ", ".join(map(str, unknown)))
    # Only high-confidence items and explicitly selected ones are considered;
    # unselected list-only items are left out of the plan entirely.
    considered = [c for c in candidates if c.action is not Action.LIST_ONLY or c.path in selected]

    snapshots = SnapshotProvider(ctx.home, ctx.runner)
    plan = execute(
        considered,
        ctx.home,
        store=store,
        snapshots=snapshots,
        selected=selected,
        allow_without_snapshot=args.allow_without_snapshot,
        dry_run=True,
    )
    _warnings(ctx, report, review)
    ctx.out(f"snapshots: {plan.snapshot_status}")
    _print_plan(ctx, plan, "Plan:")
    allowed = [by_path[o.path] for o in plan.outcomes if o.status == "would-quarantine"]
    blocked = any(o.status == "blocked" for o in plan.outcomes)
    skipped = len(candidates) - len(considered)
    if skipped:
        ctx.out(f"{skipped} list-only item(s) not considered (use --select PATH to choose).")

    if not args.apply:
        ctx.out("Dry run: nothing was changed. Add --apply to quarantine the items above.")
        return EXIT_OK
    if not allowed:
        ctx.out("Nothing to quarantine.")
        return EXIT_BLOCKED if blocked else EXIT_OK

    size = human_size(sum(c.size_bytes for c in allowed))
    if not _confirm(ctx, f"Move {len(allowed)} item(s) ({size}) to quarantine?", args.yes):
        ctx.out("Cancelled: nothing was changed.")
        return EXIT_OK
    result = execute(
        allowed,
        ctx.home,
        store=store,
        snapshots=snapshots,
        selected=selected,
        allow_without_snapshot=args.allow_without_snapshot,
        dry_run=False,
    )
    _print_plan(ctx, result, "Result:")
    if result.snapshot:
        ctx.out(f"snapshot: {result.snapshot['config']} #{result.snapshot['number']}")
    ctx.out("Restore with 'fsu restore <id>'; list with 'fsu list-quarantine'.")
    failed = any(o.status in ("blocked", "failed") for o in result.outcomes)
    return EXIT_BLOCKED if failed or blocked else EXIT_OK


def cmd_list(ctx: Context, args: argparse.Namespace) -> int:
    store = QuarantineStore(ctx.home, clock=ctx.clock)
    entries = store.entries()
    if args.json:
        json.dump([vars(m) for m in entries], ctx.stdout, ensure_ascii=False, indent=2)
        ctx.out()
        return EXIT_OK
    if not entries:
        ctx.out("Quarantine is empty.")
        return EXIT_OK
    now = ctx.clock()
    for m in entries:
        age = int((now - m.quarantined_at) / DAY)
        snap = f" snapshot {m.snapshot['config']}#{m.snapshot['number']}" if m.snapshot else ""
        state = "" if m.state == "quarantined" else f" [{m.state}]"
        ctx.out(
            f"{m.id}  {age:>4}d  {human_size(m.size_bytes):>10}  {m.original_path}{snap}{state}"
        )
    total = sum(m.size_bytes for m in entries)
    ctx.out(f"\n{len(entries)} item(s), {human_size(total)}.")
    return EXIT_OK


def _ids(store: QuarantineStore, args: argparse.Namespace) -> list[str]:
    if args.all and args.ids:
        raise UsageError("use either IDs or --all, not both")
    if not args.all and not args.ids:
        raise UsageError("give at least one ID (see 'fsu list-quarantine') or --all")
    return [m.id for m in store.entries()] if args.all else list(args.ids)


def cmd_restore(ctx: Context, args: argparse.Namespace) -> int:
    ensure_not_root()
    store = _store(ctx)
    status = EXIT_OK
    for entry_id in _ids(store, args):
        try:
            ctx.out(f"restored {entry_id} -> {store.restore(entry_id)}")
        except QuarantineError as exc:
            ctx.err(f"error: {entry_id}: {exc}")
            status = EXIT_ERROR
    return status


def cmd_purge(ctx: Context, args: argparse.Namespace) -> int:
    ensure_not_root()
    store = _store(ctx)
    if args.older_than is not None:
        if args.ids or args.all:
            raise UsageError("use either IDs, --all or --older-than")
        limit = ctx.clock() - args.older_than * DAY
        targets = [m for m in store.entries() if m.quarantined_at < limit]
    else:
        wanted = set(_ids(store, args))
        targets = [m for m in store.entries() if m.id in wanted]
        missing = wanted - {m.id for m in targets}
        if missing:
            ctx.err("error: not in quarantine: " + ", ".join(sorted(missing)))
            return EXIT_ERROR
    if not targets:
        ctx.out("Nothing to purge.")
        return EXIT_OK
    for m in targets:
        ctx.out(f"  {m.id}  {human_size(m.size_bytes):>10}  {m.original_path}")
    size = human_size(sum(m.size_bytes for m in targets))
    question = f"PERMANENTLY delete {len(targets)} item(s) ({size})? This cannot be undone."
    if not _confirm(ctx, question, args.yes):
        ctx.out("Cancelled: nothing was deleted.")
        return EXIT_OK
    status = EXIT_OK
    for m in targets:
        try:
            store.purge(m.id)
            ctx.out(f"purged {m.id}")
        except QuarantineError as exc:
            ctx.err(f"error: {m.id}: {exc}")
            status = EXIT_ERROR
    return status


# -- parser ---------------------------------------------------------------------------


def _add_ai_options(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("local AI advice (optional, off by default)")
    group.add_argument("--ai", action="store_true", help="ask a local Ollama model for advice")
    group.add_argument("--model", default=None, help="Ollama model (default: an installed one)")
    group.add_argument("--ollama-url", default=DEFAULT_URL, help=f"default {DEFAULT_URL}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fsu",
        description="Fedora Sentinel Uninstaller: safe clean-up of application residues in $HOME.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="read-only report of residue candidates")
    scan.add_argument("--json", action="store_true", help="JSON output")
    scan.add_argument("--timings", action="store_true", help="time spent per phase")
    _add_ai_options(scan)
    scan.set_defaults(func=cmd_scan)

    quarantine = sub.add_parser(
        "quarantine", help="plan (dry run) or, with --apply, move items to quarantine"
    )
    quarantine.add_argument("--apply", action="store_true", help="actually move the items")
    quarantine.add_argument("--yes", "-y", action="store_true", help="do not ask for confirmation")
    quarantine.add_argument(
        "--select",
        action="append",
        default=[],
        metavar="PATH",
        help="include a list-only item (always needs a Snapper snapshot); repeatable",
    )
    quarantine.add_argument(
        "--allow-without-snapshot",
        action="store_true",
        help="quarantine medium-confidence items even if no Snapper snapshot can be taken",
    )
    _add_ai_options(quarantine)
    quarantine.set_defaults(func=cmd_quarantine)

    listing = sub.add_parser("list-quarantine", help="items currently in quarantine")
    listing.add_argument("--json", action="store_true", help="JSON output")
    listing.set_defaults(func=cmd_list)

    restore = sub.add_parser("restore", help="move quarantined items back")
    restore.add_argument("ids", nargs="*", metavar="ID")
    restore.add_argument("--all", action="store_true", help="restore every item")
    restore.set_defaults(func=cmd_restore)

    purge = sub.add_parser("purge", help="permanently delete quarantined items")
    purge.add_argument("ids", nargs="*", metavar="ID")
    purge.add_argument("--all", action="store_true", help="every quarantined item")
    purge.add_argument(
        "--older-than", type=float, metavar="DAYS", help="items quarantined more than DAYS ago"
    )
    purge.add_argument("--yes", "-y", action="store_true", help="do not ask for confirmation")
    purge.set_defaults(func=cmd_purge)
    return parser


def main(argv: Sequence[str] | None = None, ctx: Context | None = None) -> int:
    ctx = ctx or Context()
    parser = build_parser()
    args = parser.parse_args(argv)
    func: Callable[[Context, argparse.Namespace], int] = args.func
    try:
        ensure_not_root()
        return func(ctx, args)
    except RootRefusedError as exc:
        ctx.err(f"error: {exc}")
        return EXIT_ERROR
    except UsageError as exc:
        ctx.err(f"error: {exc}")
        return EXIT_USAGE
    except (QuarantineError, ValueError) as exc:
        ctx.err(f"error: {exc}")
        return EXIT_ERROR
    except KeyboardInterrupt:
        ctx.err("interrupted: items already moved stay in quarantine and can be restored.")
        return 130


def run() -> None:  # console-script entry point
    raise SystemExit(main())


__all__ = ["Context", "build_parser", "main", "run"]
