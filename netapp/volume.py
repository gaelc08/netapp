"""'volume create' / 'volume delete' / 'volume check' orchestration: picks
the ssh or REST backend, then runs the steps common to both."""

from . import ontap_rest, ontap_ssh
from .access import print_client_access_info
from .util import error_exit


def _backend(args):
    return ontap_rest if args.api_mode == "rest" else ontap_ssh


def create(args, size_num, size_unit):
    backend = _backend(args)
    if args.volume_type == "nfs":
        mount_path = backend.create_nfs_volume(args, size_num, size_unit)
        print_client_access_info(args, "nfs", mount_path, args.export_policy)
    elif args.volume_type == "cifs":
        mount_path = backend.create_cifs_volume(args, size_num, size_unit)
        print_client_access_info(args, "cifs", mount_path)
    else:
        error_exit(f"--type must be nfs or cifs (got '{args.volume_type}')")


def delete(args):
    _backend(args).delete_volume(args)


def check_ontap_status(args):
    """ONTAP half of 'volume check' (ssh only for now): state, mount,
    export-policy rules, and the client access info block. Makes no
    changes."""
    print("")
    print("[ONTAP]")
    rows = ontap_ssh.get_ontap_fields(
        args.user, args.cluster, f"volume show -vserver {args.svm_name} -volume {args.volume_name}",
        ["policy", "junction-path", "state", "size", "used", "security-style"],
    )
    policy_name, jpath, state, size, used, sec_style = rows[0] if rows else ("", "", "", "", "", "")

    if not policy_name and not jpath and not state:
        print(f"  Volume '{args.volume_name}' NOT found on '{args.svm_name}' ({args.cluster}) - or ssh/permission issue.")
        return

    mount_status = "NOT mounted"
    if jpath and jpath != "-":
        mount_status = "mounted"
    print(f"  State           : {state or '-'}")
    print(f"  Junction-path   : {jpath or '-'} ({mount_status})")
    print(f"  Size / Used     : {size or '-'} / {used or '-'}")
    print(f"  Policy          : {policy_name or '-'}")

    if state != "online":
        print("  WARNING: volume is not online.")
    if mount_status == "NOT mounted":
        print("  WARNING: volume has no junction-path (not mounted).")

    if policy_name:
        print("")
        print(f"  Export-policy '{policy_name}' rules:")
        result = ontap_ssh.ssh_capture(
            args.user, args.cluster,
            f"export-policy rule show -vserver {args.svm_name} -policyname {policy_name} -fields clientmatch,protocol",
            combine_stderr=True,
        )
        for line in result.stdout.replace("\r", "").splitlines():
            print(f"    {line}")

    # Protocol for the access-info block below: use --type if given, else
    # infer from security-style (set explicitly to ntfs for CIFS volumes;
    # NFS volumes keep the SVM's unix/mixed default). This is a best-effort
    # guess when --type isn't passed - pass --type explicitly to be sure.
    protocol_guess = getattr(args, "volume_type", None) or ""
    if not protocol_guess:
        if sec_style == "ntfs":
            protocol_guess = "cifs"
        elif sec_style in ("unix", "mixed"):
            protocol_guess = "nfs"
    print_client_access_info(args, protocol_guess, jpath or "-", policy_name)
