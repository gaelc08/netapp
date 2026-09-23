"""Client access info block printed after a create / on 'volume check'."""

from . import ontap_rest, ontap_ssh
from .config import settings
from .util import remote_host


def get_svm_lifs(args):
    if getattr(args, "api_mode", "ssh") == "rest":
        return ontap_rest.get_svm_lifs(args)
    return ontap_ssh.get_svm_lifs(args)


def print_client_access_info(args, protocol, mount_path, policy_for_lookup=None):
    """Prints the client-facing access block: SVM LIF address(es), the
    mount point to use, and a reminder that the firewall between those LIFs
    and the client server(s) needs to be opened before access will
    actually work - this is the #1 "volume created but client still can't
    reach it" gap."""
    print("")
    print("============================================")
    print(" Client access information")
    print("============================================")

    all_lifs = get_svm_lifs(args)
    lifs = [ip for ip in all_lifs if not ip.startswith(settings().cohesity_backup_network_prefix)]
    lifs_str = " ".join(lifs)

    if not all_lifs:
        print("  Could not determine the NAS server address automatically - check manually:")
        if getattr(args, "api_mode", "ssh") == "rest":
            print(f"    GET https://{remote_host(args.cluster)}/api/network/ip/interfaces?svm.name={args.svm_name}&fields=ip.address")
        else:
            print(f'    ssh -l {args.user} {remote_host(args.cluster)} "net interface show -vserver {args.svm_name} -fields address"')
    elif not lifs:
        print(f"  No client-facing NAS server address available for {args.svm_name} - check the SVM's LIF configuration.")
    else:
        print(f"  NAS server address: {lifs_str}")

    if protocol == "nfs":
        cm = getattr(args, "client_match", None) or ""
        if not cm and policy_for_lookup:
            rows = ontap_ssh.get_ontap_fields(
                args.user, args.cluster,
                f"export-policy rule show -vserver {args.svm_name} -policyname {policy_for_lookup}",
                ["clientmatch"],
            )
            cm = ", ".join(row[0] for row in rows if row[0])
        print(f"  Mount point          : {mount_path}")
        print("  Example mount command: mount -t nfs4 <LIF_IP>:%s /local/mount/point" % mount_path)
        print("")
        print("  ACTION REQUIRED: ask the firewall team to open port 2049/TCP (NFS) between:")
        print(f"    NAS server : {lifs_str or '<see above>'}")
        print(f"    Client(s)  : {cm or '<client IP(s)>'}")
        print("  The client will not be able to mount until that's open.")
    elif protocol == "cifs":
        print(f"  Junction-path (share target): {mount_path}")
        print(r"  UNC path once a share exists: \\<LIF_IP>\<share_name>")
        print("")
        print("  ACTION REQUIRED: ask the firewall team to open port 445/TCP (CIFS/SMB) between:")
        print(f"    NAS server : {lifs_str or '<see above>'}")
        print("    Client(s)  : <client IP(s) that need access>")
        print("  The client will not be able to connect until that's open. If no CIFS")
        print("  share exists yet on this junction-path, one must be created first.")
    else:
        print("  Protocol unknown - pass --type nfs|cifs (on --check) for protocol-specific access info.")
