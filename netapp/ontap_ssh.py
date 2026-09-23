"""ONTAP over ssh (the default --api): runs the same CLI commands an admin
would type by hand, authenticated by the caller's own ssh key/agent."""

import subprocess
import sys
import time

from . import rollback
from .util import (confirm_volume_delete, error_exit, remote_host, report_export_policy_deleted,
                   report_export_policy_still_used, write_through)
from .validation import nfs_size_and_snapshot_reserve, padded_size


def ssh_capture(user, cluster, remote_cmd, combine_stderr=False):
    """Run a single remote command over ssh and capture its output."""
    cmd = ["ssh", "-o", "ConnectTimeout=5", "-l", user, remote_host(cluster), remote_cmd]
    try:
        if combine_stderr:
            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        else:
            result = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        error_exit("ssh command not found. Please install OpenSSH client.")
    return result


def run_step(args, description, remote_cmd):
    """Executes a single remote command, checking its actual exit status
    (unlike chaining several ONTAP commands in one ssh call, where only the
    last command's exit status is visible). Prints the remote command's
    output either way. Returns True on success.

    In --dry-run mode, nothing is actually sent over ssh - the command that
    would have run is printed instead, and success is simulated so the rest
    of the flow (including what it would print) plays out normally."""
    print(f"[INFO] {description}...")
    if getattr(args, "dry_run", False):
        print(f"[DRY-RUN] Would run on {args.cluster}: {remote_cmd}")
        return True
    result = ssh_capture(args.user, args.cluster, remote_cmd)
    write_through(sys.stdout, result.stdout)
    write_through(sys.stderr, result.stderr)
    return result.returncode == 0


def push_rollback(args, cmd):
    """Registers an ONTAP CLI command that undoes the step just completed."""
    rollback.push(
        cmd,
        lambda: ssh_capture(args.user, args.cluster, f"set -confirmations off; {cmd}").returncode == 0,
    )


# ── Generic ONTAP field query (showseparator + header-driven parsing) ──────
# ONTAP's column order for "-fields a,b,c" output has proven NOT reliably
# predictable - neither request order nor alphabetical held consistently
# across real tests. Rather than guess a position, this uses
# "set -showseparator :" so ONTAP prints an explicit short-name header row
# first, then reads THAT to find each field's real column index - correct
# no matter what order ONTAP actually used, in a single call.
def parse_ontap_fields(raw, fields):
    """Parses "set -showseparator :" + "-fields" output. Returns a list of
    tuples, one per data row, values in the same order as `fields`, "" for
    any field ONTAP didn't return."""
    colidx = {}
    got_header = False
    got_long = False
    rows = []

    for line in raw.replace("\r", "").split("\n"):
        parts = line.split(":")
        if not got_header:
            if parts[0] == "vserver":
                for i, name in enumerate(parts, start=1):
                    colidx[name] = i
                got_header = True
            continue
        if not got_long:
            got_long = True
            continue
        if len(parts) > 1:
            row = []
            for f in fields:
                idx = colidx.get(f)
                val = parts[idx - 1] if idx else ""
                row.append(val)
            rows.append(tuple(row))

    return rows


def get_ontap_fields(user, cluster, cmd, fields):
    """Returns a list of tuples, one per matching data row (there can be
    more than one - e.g. multiple LIFs, multiple export-policy rules),
    values in the same order as `fields`, "" for any field ONTAP didn't
    return. Returns [] if the command matched no rows."""
    remote_cmd = f"set -showseparator :; {cmd} -fields {','.join(fields)}"
    result = ssh_capture(user, cluster, remote_cmd, combine_stderr=True)
    return parse_ontap_fields(result.stdout, fields)


def execute_ssh_commands(user, host, remote_cmd, dry_run=False):
    """Non-critical/read-only "show" commands after a volume is confirmed
    created - failures there are just warnings, nothing to roll back."""
    if dry_run:
        print(f"[DRY-RUN] Would run informational lookup on {host}: {remote_cmd}")
        return
    result = ssh_capture(user, host, remote_cmd)
    write_through(sys.stdout, result.stdout)
    if result.returncode != 0:
        print(f"[WARN] Non-critical SSH command failed on {host} (volume itself is already created, nothing to roll back)", file=sys.stderr)


def get_svm_lifs(args):
    rows = get_ontap_fields(args.user, args.cluster, f"net interface show -vserver {args.svm_name}", ["address"])
    return [row[0] for row in rows if row[0]]


def list_aggregates(args):
    """Raw ONTAP tabular output: Aggregate, Size, Available, Used%, State,
    #Vols, Nodes, RAID Status - already includes TB occupancy and volume
    count per aggregate."""
    result = ssh_capture(args.user, args.cluster, "storage aggregate show")
    if result.returncode != 0:
        error_exit(f"Could not fetch aggregate list from {args.cluster}")
    return result.stdout.replace("\r", "")


def create_nfs_volume(args, size_num, size_unit):
    """Returns the volume's junction-path."""
    vol_size_calculated, snapshot_space = nfs_size_and_snapshot_reserve(args.snap_policy, size_num, size_unit)
    jct_path = f"/{args.volume_name}"
    tiering_opt = f"-tiering-policy {args.tiering_policy}" if args.tiering_policy else ""

    print(f"[INFO] Creating NFS volume {args.volume_name}...")

    if not run_step(args, f"Creating export policy {args.export_policy}",
                    f"export-policy create -vserver {args.svm_name} -policyname {args.export_policy}"):
        error_exit("Failed to create export policy - nothing was created, aborting.")
    push_rollback(args, f"export-policy delete -vserver {args.svm_name} -policyname {args.export_policy}")

    if not run_step(
        args, f"Creating export policy rule for {args.client_match}",
        f"export-policy rule create -vserver {args.svm_name} -policyname {args.export_policy} "
        f"-clientmatch {args.client_match} -rorule any -rwrule any -protocol nfs4 -superuser sys",
    ):
        rollback.run(args)
        error_exit("Failed to create export policy rule - rolled back, nothing left over on %s." % args.cluster)

    create_cmd = (
        f"volume create -vserver {args.svm_name} -volume {args.volume_name} -aggregate {args.aggregate} "
        f"-size {vol_size_calculated} -state online -policy {args.export_policy} {tiering_opt} "
        f"-is-space-reporting-logical true -is-space-enforcement-logical true -space-guarantee none "
        f"-snapshot-policy {args.snap_policy} -percent-snapshot-space {snapshot_space} "
        f'-junction-path {jct_path} -comment "{args.comment}"'
    )
    if not run_step(args, f"Creating volume {args.volume_name}", create_cmd):
        rollback.run(args)
        error_exit(f"Failed to create volume - rolled back, nothing left over on {args.cluster}.")

    print(f"[INFO] Volume {args.volume_name} created successfully.")
    print(f"[INFO] Junction-path: {jct_path}")

    # Informational only from here on - the volume already exists and is
    # usable, so a failure below is a warning, not something to roll back.
    execute_ssh_commands(
        args.user, args.cluster,
        f"export-policy rule show -vserver {args.svm_name} -policyname {args.export_policy} -fields clientmatch,protocol; "
        f"vol show -vserver {args.svm_name} -volume {args.volume_name} -fields total,junction-path; "
        f"net interface show -vserver {args.svm_name} -fields address",
        dry_run=getattr(args, "dry_run", False),
    )
    return jct_path


def create_cifs_volume(args, size_num, size_unit):
    """Returns the volume's junction-path."""
    vol_size_calculated = padded_size(size_num, size_unit)
    tiering_opt = f"-tiering-policy {args.tiering_policy}" if args.tiering_policy else ""

    print(f"[INFO] Creating CIFS volume {args.volume_name}...")

    create_cmd = (
        f"volume create -vserver {args.svm_name} -volume {args.volume_name} -aggregate {args.aggregate} "
        f"-size {vol_size_calculated} -state online -policy default {tiering_opt} "
        f"-is-space-reporting-logical true -is-space-enforcement-logical true -space-guarantee none "
        f"-security-style ntfs -snapshot-policy {args.snap_policy} -percent-snapshot-space 10 "
        f'-comment "{args.comment}"'
    )
    if not run_step(args, f"Creating volume {args.volume_name}", create_cmd):
        error_exit("Failed to create volume - nothing was created, aborting.")
    push_rollback(
        args,
        f"set -confirmations off; volume offline -vserver {args.svm_name} -volume {args.volume_name}; "
        f"volume delete -vserver {args.svm_name} -volume {args.volume_name}",
    )

    print("[INFO] Waiting for operations to complete...")
    time.sleep(2)

    if not run_step(
        args, f"Mounting volume at {args.junction_path}",
        f"mount -volume {args.volume_name} -vserver {args.svm_name} -junction-path {args.junction_path} -active true",
    ):
        rollback.run(args)
        error_exit(f"Failed to mount volume - rolled back (volume deleted), nothing left over on {args.cluster}.")

    # Home-directory volumes get a default 15GB per-user quota
    if args.volume_name.startswith("home"):
        print("[INFO] Volume name starts with 'home' - applying default 15GB user quota...")

        if not run_step(
            args, "Creating quota rule (15GB per user)",
            f'volume quota policy rule create -vserver {args.svm_name} -policy-name default -volume '
            f'{args.volume_name} -type user -target "" -qtree "" -disk-limit 15GB',
        ):
            rollback.run(args)
            error_exit(f"Failed to create quota rule - rolled back (volume deleted), nothing left over on {args.cluster}.")

        if not run_step(args, "Enabling quota", f"volume quota on -vserver {args.svm_name} -volume {args.volume_name}"):
            rollback.run(args)
            error_exit(f"Failed to enable quota - rolled back (volume deleted), nothing left over on {args.cluster}.")

        time.sleep(5)
        # Quota resize failing is non-fatal - quota is already enabled at
        # this point, just not immediately resized. Not worth a full rollback.
        if not run_step(args, "Resizing quota", f"volume quota resize -vserver {args.svm_name} -volume {args.volume_name}"):
            print("[WARN] Quota resize failed - quota is enabled but may need a manual resize later.", file=sys.stderr)
        print("[INFO] Quota set for home volume.")

    print(f"[INFO] Volume {args.volume_name} created and mounted successfully.")
    print(f"[INFO] Junction-path: {args.junction_path}")
    return args.junction_path


def delete_volume(args):
    print(f"[INFO] Looking up volume {args.volume_name} on {args.svm_name} ({args.cluster})...")
    rows = get_ontap_fields(args.user, args.cluster, f"volume show -vserver {args.svm_name} -volume {args.volume_name}",
                            ["policy", "junction-path"])
    policy_name, jpath = rows[0] if rows else ("", "")

    if not policy_name:
        error_exit(f"Could not find volume '{args.volume_name}' on vserver '{args.svm_name}' - aborting, nothing touched.")

    print(f"[INFO] Found volume '{args.volume_name}' - export-policy: '{policy_name}', junction-path: '{jpath or '-'}'.")

    confirm_volume_delete(args)

    if jpath and jpath != "-":
        if not run_step(args, "Unmounting volume", f"volume unmount -vserver {args.svm_name} -volume {args.volume_name}"):
            error_exit("Failed to unmount volume - aborting before delete, nothing else touched.")

    if not run_step(args, "Taking volume offline", f"volume offline -vserver {args.svm_name} -volume {args.volume_name}"):
        error_exit("Failed to take volume offline - aborting before delete.")

    if not run_step(args, "Deleting volume",
                    f"set -confirmations off; volume delete -vserver {args.svm_name} -volume {args.volume_name}"):
        error_exit("Failed to delete volume. It is now offline but still present - check manually on %s." % args.cluster)

    print(f"[INFO] Volume '{args.volume_name}' deleted.")

    if policy_name in ("default", "none"):
        print(f"[INFO] Export-policy '{policy_name}' is a built-in ONTAP policy - not touching it.")
        return

    # Safety check: only delete the export-policy if no OTHER volume on
    # this vserver still references it (the volume we just deleted is
    # already gone from this listing, so no need to exclude it manually).
    vol_rows = get_ontap_fields(args.user, args.cluster, f"volume show -vserver {args.svm_name}", ["volume", "policy"])
    other_users = [v for v, p in vol_rows if p == policy_name]
    if report_export_policy_still_used(args, policy_name, other_users):
        return

    ok = run_step(args, f"Deleting export-policy {policy_name}",
                  f"export-policy delete -vserver {args.svm_name} -policyname {policy_name}")
    report_export_policy_deleted(args, policy_name, ok)
