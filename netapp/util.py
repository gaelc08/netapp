"""Small helpers shared by every module."""

import sys

from .constants import DOMAIN

# requests is only needed for --api rest and Cohesity; the default ssh path
# works without it, so a missing install is reported where it matters
# instead of failing at import time.
try:
    import requests
    from requests.packages.urllib3.exceptions import InsecureRequestWarning
    requests.packages.urllib3.disable_warnings(InsecureRequestWarning)
    HAVE_REQUESTS = True
except ImportError:
    requests = None
    HAVE_REQUESTS = False


def error_exit(message):
    print(f"[ERROR] {message}", file=sys.stderr)
    sys.exit(1)


def remote_host(cluster):
    return f"{cluster}.{DOMAIN}"


def format_bytes_human(n):
    """Formats a byte count the way ONTAP's own CLI tables do (e.g. "1.75TB",
    "512MB") instead of a raw byte integer. Passes through anything that
    isn't a plain number (e.g. the "-" placeholder for a missing value)."""
    try:
        size = float(n)
    except (TypeError, ValueError):
        return str(n)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(size) < 1024.0 or unit == "PB":
            return f"{int(size)}{unit}" if unit == "B" else f"{size:.2f}{unit}"
        size /= 1024.0


def write_through(stream, text):
    """Echoes a remote command's captured output, newline-terminated."""
    if text:
        stream.write(text)
        if not text.endswith("\n"):
            stream.write("\n")


# ── volume delete, shared by the ssh and REST paths ─────────────────────────
def confirm_volume_delete(args):
    """Asks for "DELETE" before an irreversible volume delete, unless --yes
    or --dry-run. Exits (status 0) if not confirmed."""
    if getattr(args, "dry_run", False):
        print(f"[DRY-RUN] Would prompt to confirm deletion of '{args.volume_name}', then run the steps below.")
    elif not args.assume_yes:
        print("")
        confirm = input(
            f"Delete volume '{args.volume_name}' on {args.svm_name} ({args.cluster})? This is IRREVERSIBLE. Type DELETE to confirm: "
        )
        if confirm != "DELETE":
            print("Aborted. Nothing was touched.")
            sys.exit(0)


def report_export_policy_still_used(args, policy_name, other_users):
    """Returns True (after saying so) if other volumes still use the policy."""
    if not other_users:
        return False
    print(f"[WARN] Export-policy '{policy_name}' is still used by other volume(s) on {args.svm_name} - NOT deleting it:")
    for v in other_users:
        print(f"  - {v}")
    return True


def report_export_policy_deleted(args, policy_name, ok):
    if not ok:
        print(f"[WARN] Could not delete export-policy '{policy_name}' - may need manual cleanup on {args.cluster}.", file=sys.stderr)
    else:
        print(f"[INFO] Export-policy '{policy_name}' deleted.")
