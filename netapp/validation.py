"""Input parsing and validation for 'volume create'."""

import re

from .constants import SNAP_POLICY_PROTOCOL_RESTRICTIONS, VALID_SIZE_UNITS, VALID_SNAP_POLICIES
from .util import error_exit

SIZE_WITH_UNIT_RE = re.compile(r"^([0-9]+(\.[0-9]+)?)([A-Za-z]+)$")
SIZE_BARE_RE = re.compile(r"^([0-9]+(\.[0-9]+)?)$")
VOLUME_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def parse_size(vol_size):
    """Parses --size into a numeric part and a unit (GB/TB/MB/...). Accepts
    a bare number (assumed GB, e.g. "100") or a number with a unit attached
    (e.g. "100GB", "2TB") - returns (size_num, size_unit), used for both the
    volume size itself and the 10/9 snapshot-space padding calculation (kept
    in the same unit so the arithmetic stays correct regardless of which
    unit was given)."""
    m = SIZE_WITH_UNIT_RE.match(vol_size)
    if m:
        size_num = m.group(1)
        size_unit = m.group(3).upper()
    elif SIZE_BARE_RE.match(vol_size):
        size_num = vol_size
        size_unit = "GB"
    else:
        error_exit("--size must be a positive number (decimals allowed), optionally followed by a unit (e.g. 100, 1.5TB)")

    if size_unit not in VALID_SIZE_UNITS:
        error_exit(f"--size unit must be one of: {', '.join(VALID_SIZE_UNITS)} (got '{size_unit}')")

    return size_num, size_unit


def padded_size(size_num, size_unit):
    """Size to request from ONTAP so that, after the 10% snapshot reserve,
    the usable space is the size that was asked for."""
    return f"{float(size_num) * 10 / 9:.2f}{size_unit}"


def nfs_size_and_snapshot_reserve(snap_policy, size_num, size_unit):
    """NFS volumes with no snapshot policy get no snapshot reserve (and so
    no padding); CIFS volumes always use padded_size() with a 10% reserve."""
    if snap_policy == "none":
        return f"{size_num}{size_unit}", 0
    return padded_size(size_num, size_unit), 10


def validate_inputs(args):
    if args.snap_policy not in VALID_SNAP_POLICIES:
        error_exit(f"snap_policy must be one of: {' '.join(VALID_SNAP_POLICIES)}")

    restricted_to = SNAP_POLICY_PROTOCOL_RESTRICTIONS.get(args.snap_policy)
    if restricted_to and args.volume_type != restricted_to:
        error_exit(f"--snap-policy {args.snap_policy} is only valid for --type {restricted_to} volumes (got --type {args.volume_type})")

    # Validate volume name against ONTAP's own naming rule (alphanumeric
    # and underscore only, no hyphens/spaces/etc). Checking this ourselves
    # avoids a silent-looking "success" message: because each ssh call for
    # a volume is a single command, an ONTAP-side rejection of "volume
    # create" is caught by its own exit status - but a bad name is worth
    # rejecting up front with a clear message.
    if not VOLUME_NAME_RE.match(args.volume_name):
        error_exit(
            "volume name can only contain letters, digits, and underscores, and must "
            f"start with a letter or underscore (no hyphens) - got '{args.volume_name}'"
        )

    if args.volume_type == "nfs":
        if not args.client_match:
            error_exit("For NFS volumes, --client-match is required")
    elif args.volume_type == "cifs":
        if not args.junction_path:
            error_exit("For CIFS volumes, --junction-path is required")
