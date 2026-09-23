"""Command-line interface: argument parsing and subcommand dispatch."""

import argparse
import sys

from . import __version__, cohesity, rollback, volume
from .aggregate import list_aggregates, select_aggregate_interactively
from .util import error_exit, silence_insecure_request_warnings
from .validation import parse_size, validate_inputs

TOP_DESCRIPTION = """\
NetApp ONTAP volume management, with optional Cohesity backup registration.

Commands:
  netapp aggregate list     Show aggregate occupancy
  netapp volume create      Create an NFS or CIFS volume (registers it with
                             Cohesity afterward, unless --no-backup)
  netapp volume delete      Delete a volume (and its export-policy, if safe)
  netapp volume check       Read-only ONTAP + Cohesity status report
  netapp volume backup      Register an already-existing volume with Cohesity
                             (the recovery path after a failed/skipped backup
                             step on 'volume create' - do NOT re-run the full
                             create in that case, it fails on 'export-policy
                             already exists')

Run 'netapp <command> -h' for that command's own options."""

TOP_EPILOG = """\
Notes that apply across commands:

--size (volume create) accepts a bare number (assumed GB, e.g. 100) or a
number with a unit attached (e.g. 100GB, 2TB). Valid units: B, KB, MB, GB,
TB, PB.

--aggregate (volume create) is optional. If omitted, the script fetches
aggregate occupancy from --cluster and prompts you to pick one.

--tiering-policy (volume create) is optional and OMITTED by default (volume
keeps the aggregate's own tiering behavior). Only pass it on FabricPool-
enabled aggregates/clusters - ONTAP rejects it on an aggregate with no
object store attached.

--dry-run (volume create/delete/backup) prints every ONTAP command that
would be run (and, for delete, skips the confirmation prompt) instead of
sending it, and skips all Cohesity API calls entirely - it just prints
which job the volume would be registered to, so no Cohesity API key is
needed for a dry run. Read-only lookups (e.g. looking up an existing volume
before a delete, or SVM LIF addresses for the client access info block)
still run for real, since they make no changes.

--api <ssh|rest> selects how ONTAP itself is reached. ssh (the default) runs
the same CLI commands an admin would type by hand. rest talks to ONTAP's
REST API over HTTPS instead - implemented for aggregate list, volume create
(NFS and CIFS, including the tiering-policy PATCH, the mount step and
home-directory quota for CIFS), and volume delete, using async job polling
where ONTAP requires it. volume check is not yet ported to REST and rejects
--api rest for now.

ONTAP REST authentication (only relevant with --api rest), tried in order:
  1. Client certificate, if configured: [--cert-file <path>] [--key-file <path>]
     (or $ONTAP_CERT_FILE / $ONTAP_KEY_FILE) - both required together. The
     certificate must already be installed in ONTAP and mapped to a user
     (security login create -authmethod cert).
  2. HTTP basic auth otherwise, using --user plus [--password <password>]
     (falls back to $ONTAP_PASSWORD, then a hidden prompt).
  [--ca-bundle <path>]     trust a private/internal CA (default: normal TLS verification)
  [--insecure-ontap]       skip TLS verification entirely - not recommended

Cohesity backup (volume create/check/backup):
  [--backup-tier <short|mid|long|none>]   prompted interactively if omitted; 'none' = skip
  [--cohesity-apikey <apikey>]            falls back to $COHESITY_APIKEY, then a prompt
  [--cohesity-cluster <name>]             override auto-detected Cohesity cluster
  [--cohesity-job <name>]                 override auto-built job name
  [--cohesity-ca-bundle <path>]           trust a private/internal CA for the Cohesity API
                                          (falls back to $COHESITY_CA_BUNDLE; default:
                                          normal TLS verification)
  [--insecure-cohesity]                   skip TLS verification for Cohesity - not recommended
  [--no-backup]                           (create only) skip Cohesity protection entirely

The Cohesity cluster and job name are derived from --cluster and --backup-tier:
  damascus-3, jericho-1 -> closluce-1, job '<Cluster>-<Tier>Term-DC1'
  damascus-4, jericho-2 -> closluce-2, job '<Cluster>-<Tier>Term-CS3'
For any other --cluster, both --cohesity-cluster and --cohesity-job must be
given explicitly, or backup registration is skipped with a warning.

Examples:
  netapp aggregate list --user admin --cluster cluster1

  netapp volume create --type nfs --user admin --cluster cluster1 --svm svm1 \\
      --volume vol1 --size 100 --aggregate aggr1 --snap-policy CTIE_default \\
      --client-match 10.0.0.0/24 --comment 'Test volume'

  netapp volume create --type cifs --user admin --cluster cluster1 --svm svm1 \\
      --volume vol1 --size 100 --aggregate aggr1 --snap-policy CTIE_default \\
      --junction-path /share1 --comment 'Test volume'

  netapp volume delete --user admin --cluster cluster1 --svm svm1 --volume vol1

  netapp volume check --user admin --cluster cluster1 --svm svm1 --volume vol1
"""


def _common_ontap_parser():
    """ONTAP connection/auth/dry-run/confirmation flags, shared by every
    subcommand that talks to ONTAP and/or Cohesity."""
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--user")
    p.add_argument("--cluster")
    p.add_argument("--api", dest="api_mode", default="ssh")
    p.add_argument("--cert-file", dest="ontap_cert_file")
    p.add_argument("--key-file", dest="ontap_key_file")
    p.add_argument("--password", dest="ontap_password")
    p.add_argument("--ca-bundle", dest="ontap_ca_bundle")
    p.add_argument("--insecure-ontap", dest="ontap_insecure", action="store_true")
    p.add_argument("--dry-run", dest="dry_run", action="store_true")
    p.add_argument("--yes", dest="assume_yes", action="store_true")
    return p


def _cohesity_parser():
    """Cohesity backup-registration flags, shared by 'volume create',
    'volume check', and 'volume backup'."""
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--backup-tier", dest="backup_tier")
    p.add_argument("--cohesity-apikey", dest="cohesity_apikey")
    p.add_argument("--cohesity-cluster", dest="cohesity_cluster_override")
    p.add_argument("--cohesity-job", dest="cohesity_job_override")
    p.add_argument("--cohesity-ca-bundle", dest="cohesity_ca_bundle")
    p.add_argument("--insecure-cohesity", dest="cohesity_insecure", action="store_true")
    return p


def build_parser():
    common = _common_ontap_parser()
    cohesity = _cohesity_parser()

    parser = argparse.ArgumentParser(
        prog="netapp",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=TOP_DESCRIPTION,
        epilog=TOP_EPILOG,
    )
    parser.add_argument("--version", action="version", version=f"netapp version {__version__}")

    resources = parser.add_subparsers(dest="resource", required=True, metavar="<resource>")

    volume = resources.add_parser("volume", help="Manage NetApp volumes")
    volume_actions = volume.add_subparsers(dest="action", required=True, metavar="<action>")

    create = volume_actions.add_parser(
        "create", parents=[common, cohesity], help="Create an NFS or CIFS volume",
        description="Create an NFS or CIFS volume, then register it with Cohesity unless --no-backup.",
    )
    create.add_argument("--type", dest="volume_type")
    create.add_argument("--svm", dest="svm_name")
    create.add_argument("--volume", dest="volume_name")
    create.add_argument("--size", dest="vol_size")
    create.add_argument("--aggregate")
    create.add_argument("--snap-policy", dest="snap_policy")
    create.add_argument("--tiering-policy", dest="tiering_policy")
    create.add_argument("--export-policy", dest="export_policy")
    create.add_argument("--client-match", dest="client_match")
    create.add_argument("--junction-path", dest="junction_path")
    create.add_argument("--comment")
    create.add_argument("--no-backup", dest="no_backup", action="store_true")

    delete = volume_actions.add_parser(
        "delete", parents=[common, cohesity], help="Delete a volume (and its export-policy, if safe)",
        description="Delete a volume (unmounting first if needed) and, if safe (no other "
                     "volume on the vserver still uses it), its export-policy too. Asks for "
                     "confirmation (type DELETE) unless --yes. Afterward, also removes the "
                     "volume from any Cohesity protection job it was in (unless --no-unprotect) "
                     "- this does NOT delete any backup data already taken, only the stale "
                     "registration.",
    )
    delete.add_argument("--svm", dest="svm_name")
    delete.add_argument("--volume", dest="volume_name")
    delete.add_argument("--no-unprotect", dest="no_unprotect", action="store_true")

    check = volume_actions.add_parser(
        "check", parents=[common, cohesity], help="Read-only ONTAP + Cohesity status report",
        description="Read-only status report: ONTAP state/mount/export-policy rules, "
                     "Cohesity discovery and protection job(s), and client access info. "
                     "Makes NO changes anywhere - safe to run any time.",
    )
    check.add_argument("--svm", dest="svm_name")
    check.add_argument("--volume", dest="volume_name")
    check.add_argument("--type", dest="volume_type")

    backup = volume_actions.add_parser(
        "backup", parents=[common, cohesity], help="Register an already-existing volume with Cohesity",
        description="Registers an ALREADY-EXISTING volume with Cohesity - no ONTAP call is "
                     "made, --user is not required. Use this to retry Cohesity registration "
                     "after 'volume create' succeeded but its backup step failed.",
    )
    backup.add_argument("--svm", dest="svm_name")
    backup.add_argument("--volume", dest="volume_name")

    aggregate = resources.add_parser("aggregate", help="Query NetApp aggregates")
    aggregate_actions = aggregate.add_subparsers(dest="action", required=True, metavar="<action>")
    aggregate_actions.add_parser(
        "list", parents=[common], help="List aggregate occupancy",
        description="Prints aggregate occupancy (size, available, used, state, #vols) for --cluster.",
    )

    return parser


# Maps an internal dest name back to the flag a user would actually type,
# for require_args()'s error messages - dest and flag diverge for several
# (e.g. dest "svm_name" is flag "--svm", not "--svm-name").
ARG_FLAG_NAMES = {
    "user": "--user",
    "cluster": "--cluster",
    "svm_name": "--svm",
    "volume_name": "--volume",
    "volume_type": "--type",
    "vol_size": "--size",
    "snap_policy": "--snap-policy",
}


def require_args(args, names):
    for name in names:
        if not getattr(args, name, None):
            flag = ARG_FLAG_NAMES.get(name, "--" + name.replace("_", "-"))
            error_exit(f"Missing required argument: {flag}")


def main(argv=None):
    parser = build_parser()
    args, unknown = parser.parse_known_args(argv)
    if unknown:
        error_exit(f"Unknown option: {unknown[0]}")
    rollback.clear()

    # Defaults for optional parameters - only meaningful for "volume
    # create", the only subcommand where these flags exist at all.
    if not getattr(args, "comment", None):
        args.comment = "Created by netapp_volume script"
    if not getattr(args, "export_policy", None):
        args.export_policy = getattr(args, "volume_name", None)

    if getattr(args, "ontap_insecure", False) or getattr(args, "cohesity_insecure", False):
        silence_insecure_request_warnings()

    if args.api_mode not in ("ssh", "rest"):
        error_exit(f"--api must be ssh or rest (got '{args.api_mode}')")

    if args.dry_run:
        print("[INFO] --dry-run: no ONTAP or Cohesity changes will be made. Commands that would run are printed with a [DRY-RUN] prefix.")

    command = (args.resource, args.action)

    # aggregate list: just show occupancy and exit, no volume created
    if command == ("aggregate", "list"):
        require_args(args, ["user", "cluster"])
        print(list_aggregates(args), end="")
        sys.exit(0)

    # volume delete: remove a volume (and its export-policy, if safe) and exit
    if command == ("volume", "delete"):
        require_args(args, ["user", "cluster", "svm_name", "volume_name"])
        volume.delete(args)
        if not args.no_unprotect:
            cohesity.unprotect_volume(args)
        sys.exit(0)

    # volume backup: register an ALREADY-EXISTING volume with Cohesity and
    # exit - no ONTAP call is made at all (--api is irrelevant here). This
    # is the recovery path if volume creation succeeded but the Cohesity
    # step failed/was skipped (bad --cohesity-apikey, job not found yet,
    # etc.) - re-running 'volume create' in that situation just fails on
    # "export-policy already exists". --user is intentionally not required
    # here since nothing touches ONTAP in this mode.
    if command == ("volume", "backup"):
        require_args(args, ["cluster", "svm_name", "volume_name"])
        cohesity.protect_volume(args)
        sys.exit(0)

    # volume check: read-only status report (ONTAP + Cohesity), no changes made
    if command == ("volume", "check"):
        require_args(args, ["user", "cluster", "svm_name", "volume_name"])
        if args.api_mode == "rest":
            error_exit("--api rest is not yet implemented for 'volume check'; use --api ssh (the default).")
        print("============================================")
        print(f" Status check: {args.volume_name} on {args.svm_name} ({args.cluster})")
        print("============================================")
        volume.check_ontap_status(args)
        cohesity.print_protection_status(args)
        sys.exit(0)

    # volume create: validate required arguments (aggregate is NOT required
    # here - if missing, we prompt interactively below instead of failing)
    require_args(args, ["volume_type", "user", "cluster", "svm_name", "volume_name", "vol_size", "snap_policy"])

    # Parse --size into size_num / size_unit (accepts "100" or "100GB")
    size_num, size_unit = parse_size(args.vol_size)

    # If no aggregate given, show occupancy and ask which one to use
    if not args.aggregate:
        args.aggregate = select_aggregate_interactively(args)

    validate_inputs(args)

    volume.create(args, size_num, size_unit)

    print("[INFO] Volume creation completed successfully.")

    # Register the new volume with Cohesity, unless explicitly skipped.
    # Never fatal - the volume above is already created regardless of what
    # happens here.
    if not args.no_backup:
        cohesity.protect_volume(args)
