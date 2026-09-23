"""Single undo stack for a volume create, shared by the ssh and REST paths.

Each entry is (description, zero-arg callable returning True on success).
If a later step fails partway through, run() undoes whatever already
succeeded, most recent first."""

import sys

_STEPS = []


def push(description, fn):
    _STEPS.append((description, fn))


def clear():
    _STEPS.clear()


def run(args):
    if not _STEPS:
        return
    print(f"[WARN] Rolling back {len(_STEPS)} already-completed step(s) on {args.cluster}...", file=sys.stderr)
    while _STEPS:
        description, fn = _STEPS.pop()
        print(f"[WARN] Rollback: {description}", file=sys.stderr)
        try:
            ok = fn()
        except Exception as exc:
            ok = False
            print(f"[WARN] Rollback step raised an error: {exc}", file=sys.stderr)
        if not ok:
            print(f"[WARN] Rollback step itself failed - MANUAL CLEANUP NEEDED on {args.cluster}: {description}", file=sys.stderr)
