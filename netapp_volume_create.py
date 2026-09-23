#!/usr/bin/env python3
"""netapp_volume_create.py

Python port of netapp_volume_create.sh.
Version: 2026-09-23-05 (Python port of bash SCRIPT_VERSION 2026-09-09-17)
"""

import argparse
import getpass
import json
import os
import re
import subprocess
import sys
import time

try:
    import requests
    from requests.packages.urllib3.exceptions import InsecureRequestWarning
    requests.packages.urllib3.disable_warnings(InsecureRequestWarning)
    HAVE_REQUESTS = True
except ImportError:
    HAVE_REQUESTS = False

SCRIPT_VERSION = "2026-09-23-05"

# LIF addresses on this network are reserved for Cohesity backup traffic
# and must never be handed out to clients as a mount target - excluded
# from the client access info block below.
COHESITY_BACKUP_NETWORK_PREFIX = "10.111.210."

VALID_SNAP_POLICIES = [
    "CTIE_daily", "CTIE_daily_315", "CTIE_default", "CTIE_heavy",
    "CTIE_light", "CTIE_medium", "CTIE_one_weekly", "none",
]
VALID_SIZE_UNITS = ["B", "KB", "MB", "GB", "TB", "PB"]

# cluster -> (cohesity_cluster, job-name suffix)
COHESITY_CLUSTER_MAP = {
    "damascus-3": ("closluce-1", "DC1"),
    "jericho-1": ("closluce-1", "DC1"),
    "damascus-4": ("closluce-2", "CS3"),
    "jericho-2": ("closluce-2", "CS3"),
}

# Rollback stack: remote commands to run, in reverse order, to undo
# whatever already succeeded if a later step fails partway through.
ROLLBACK_STEPS = []


def error_exit(message):
    print(f"[ERROR] {message}", file=sys.stderr)
    sys.exit(1)


EPILOG = """\
--size accepts a bare number (assumed GB, e.g. 100) or a number with a unit
attached (e.g. 100GB, 2TB). Valid units: B, KB, MB, GB, TB, PB.

--aggregate is optional. If omitted, the script fetches aggregate occupancy
(size, available, used%%, #vols) from --cluster and prompts you to pick one.

Also: %(prog)s --list-aggregates --user <username> --cluster <source_cluster>
      Just prints aggregate occupancy for --cluster and exits, no volume created.

Also: %(prog)s --delete --user <username> --cluster <source_cluster> --svm <svm_name> --volume <volume_name>
      Deletes a volume (unmounting first if needed) and, if safe (no other
      volume on the vserver still uses the same export-policy), deletes its
      export-policy too. Asks for confirmation (type DELETE) unless --yes.

Also: %(prog)s --backup-only --cluster <source_cluster> --svm <svm_name> --volume <volume_name>
      [--backup-tier <short|mid|long|none>] [--cohesity-apikey <key>] ...
      Registers an ALREADY-EXISTING volume with Cohesity and exits - no ONTAP
      call is made, --user is not required. Use this to retry Cohesity
      registration after a volume was created successfully but the backup
      step failed (wrong API key, job not found yet, etc) - do NOT re-run the
      full create command in that case, it will fail on 'export-policy exists'.

Also: %(prog)s --check --user <username> --cluster <source_cluster> --svm <svm_name> --volume <volume_name>
      [--type <nfs|cifs>] [--cohesity-apikey <key>] [--cohesity-cluster <name>] ...
      Read-only status report: ONTAP state/mount/export-policy rules, plus
      Cohesity discovery + which protection job(s), if any, protect it, plus
      client access info (SVM LIF address(es), mount point, firewall reminder).
      --type is optional here - if omitted it's inferred from security-style,
      pass it explicitly to be sure. Makes NO changes anywhere - safe to run
      any time.

--tiering-policy is optional and OMITTED by default (volume keeps the aggregate's
own tiering behavior). Only pass it on FabricPool-enabled aggregates/clusters -
ONTAP rejects -tiering-policy on an aggregate with no object store attached.

--dry-run works with volume creation, --delete, and --backup-only. It prints
every ONTAP command that would be run (and, for --delete, skips the
confirmation prompt) instead of sending it over ssh, and skips all Cohesity
API calls entirely - it just prints which job the volume would be registered
to, so no Cohesity API key is needed for a dry run. Read-only lookups (e.g.
looking up an existing volume before a delete, or SVM LIF addresses for the
client access info block) still run for real, since they make no changes.

--api <ssh|rest> selects how ONTAP itself is reached. ssh (the default) runs
the same CLI commands an admin would type by hand. rest talks to ONTAP's
REST API over HTTPS instead - implemented for --list-aggregates, volume
creation (NFS and CIFS, including the tiering-policy PATCH, the mount step
and home-directory quota for CIFS), and --delete, using async job polling
where ONTAP requires it (volume create/mount/offline/delete, quota rule
create). --check is not yet ported to REST and rejects --api rest for now.
Endpoints and field names were taken directly from the ONTAP REST OpenAPI
spec, not guessed - still worth a --dry-run pass against a real cluster
before trusting it in production, since it hasn't been exercised against
live ONTAP from this environment.

ONTAP REST authentication (only relevant with --api rest), tried in order:
          1. Client certificate, if configured:
             [--cert-file <path>] [--key-file <path>]
             (or $ONTAP_CERT_FILE / $ONTAP_KEY_FILE) - both are required
             together. The certificate must already be installed in ONTAP
             and mapped to a user (security login create -authmethod cert).
          2. HTTP basic auth otherwise, using --user plus:
             [--password <password>]           (falls back to $ONTAP_PASSWORD, then a prompt)
          [--ca-bundle <path>]                  (trust a private/internal CA; default is normal TLS verification)
          [--insecure-ontap]                    (skip TLS verification entirely - not recommended)

Cohesity backup (on by default after volume creation, use --no-backup to skip):
          [--backup-tier <short|mid|long|none>]   (prompted interactively if omitted; 'none' = skip)
          [--cohesity-apikey <apikey>]        (falls back to $COHESITY_APIKEY, then a prompt)
          [--cohesity-cluster <name>]         (override auto-detected Cohesity cluster)
          [--cohesity-job <name>]             (override auto-built job name)
          [--no-backup]                       (skip Cohesity protection entirely)

The Cohesity cluster and job name are derived from --cluster and --backup-tier:
  damascus-3, jericho-1 -> closluce-1, job '<Cluster>-<Tier>Term-DC1'
  damascus-4, jericho-2 -> closluce-2, job '<Cluster>-<Tier>Term-CS3'
For any other --cluster, both --cohesity-cluster and --cohesity-job must be given
explicitly, or backup registration is skipped with a warning (volume is still created).

For NFS volumes:
          --export-policy <policy_name> (optional, defaults to volume name)
          --client-match <client_pattern>
          --comment <description>

For CIFS volumes:
          --junction-path <path> --comment <description>

Examples:
  NFS: %(prog)s --type nfs --user admin --cluster cluster1 --svm svm1 --volume vol1
           --size 100 --aggregate aggr1 --snap-policy CTIE_default
           --export-policy expol1 --client-match 10.0.0.0/24
           --comment 'Test volume'

  CIFS: %(prog)s --type cifs --user admin --cluster cluster1 --svm svm1 --volume vol1
           --size 100 --aggregate aggr1 --snap-policy CTIE_default
           --backup-svm backup_svm --junction-path /share1 --comment 'Test volume'
"""


def remote_host(cluster):
    return f"{cluster}.ctie.etat.lu"


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


# ── ONTAP REST API (opt-in via --api rest) ──────────────────────────────────
# Everything above (and the rest of the script) talks to ONTAP over ssh,
# running the same CLI commands an admin would type by hand - that's the
# default and the only fully implemented path. --api rest is an opt-in,
# still-partial alternative that talks to ONTAP's REST API over HTTPS
# instead. Only read-only aggregate listing is implemented on REST so far;
# volume/export-policy/quota mutations have NOT been ported to REST (their
# exact field names need verifying against the target cluster's ONTAP
# version before it's safe to write production code against them) and
# --api rest is rejected for those modes rather than silently falling back
# to ssh or guessing.
#
# Authentication (in this order):
#   1. Client certificate, if configured: --cert-file/--key-file, or
#      $ONTAP_CERT_FILE/$ONTAP_KEY_FILE. Requires the cert already installed
#      in ONTAP and mapped to a user via
#      "security login create -authmethod cert -application http ...".
#      See: https://docs.netapp.com/us-en/ontap-technical-reports/ontap-security-hardening/set-up-certificate-based-api-access.html
#   2. HTTP basic auth otherwise, using --user plus --password, or
#      $ONTAP_PASSWORD, or (if neither is given) a hidden interactive prompt
#      - the same fallback chain already used for the Cohesity API key.
def resolve_ontap_auth(args):
    """Returns a dict of kwargs to merge into a requests call: either
    {"cert": (cert_file, key_file)} for client-certificate auth, or
    {"auth": (user, password)} for HTTP basic auth."""
    cert_file = getattr(args, "ontap_cert_file", None) or os.environ.get("ONTAP_CERT_FILE")
    key_file = getattr(args, "ontap_key_file", None) or os.environ.get("ONTAP_KEY_FILE")

    if cert_file and key_file:
        if not os.path.isfile(cert_file):
            error_exit(f"--cert-file '{cert_file}' does not exist")
        if not os.path.isfile(key_file):
            error_exit(f"--key-file '{key_file}' does not exist")
        return {"cert": (cert_file, key_file)}

    if cert_file or key_file:
        error_exit("--cert-file and --key-file must both be given (or neither) for client-certificate auth")

    if not args.user:
        error_exit("ONTAP REST basic auth requires --user (no client certificate was configured)")

    password = getattr(args, "ontap_password", None) or os.environ.get("ONTAP_PASSWORD")
    if not password:
        password = getpass.getpass(f"ONTAP password for {args.user}@{args.cluster}: ")
        if not password:
            error_exit("No ONTAP password provided")
        # Cache it on args so the many REST calls in one run (including a
        # job-poll loop that can hit this every 2 seconds) reuse it instead
        # of prompting again and again.
        args.ontap_password = password

    return {"auth": (args.user, password)}


def ontap_rest_request(args, method, path, **kwargs):
    """Issues one raw ONTAP REST call against --cluster and returns the
    requests.Response. TLS is verified by default; pass --ca-bundle for an
    internal/self-signed CA, or --insecure-ontap to skip verification (not
    recommended). Raises requests.RequestException on a connection-level
    failure (DNS/connect/timeout/TLS) - callers use ontap_rest_json(), which
    catches that the same way ssh_capture() lets a failed ssh command return
    a non-zero exit code instead of crashing the script."""
    if not HAVE_REQUESTS:
        error_exit("Python 'requests' package not found - required for --api rest (pip install requests)")

    auth_kwargs = resolve_ontap_auth(args)
    if getattr(args, "ontap_insecure", False):
        verify = False
    else:
        verify = getattr(args, "ontap_ca_bundle", None) or True

    url = f"https://{remote_host(args.cluster)}/api{path}"
    return requests.request(method, url, timeout=30, verify=verify, headers={"Accept": "application/json"}, **auth_kwargs, **kwargs)


def ontap_rest_json(args, method, path, **kwargs):
    """Returns (status_code, body_dict). status_code is 0 (with a "message"
    in body) if the request itself could not be sent at all - callers treat
    that the same as any other non-2xx failure, mirroring how a failed ssh
    connection just shows up as a non-zero ssh_capture().returncode."""
    try:
        resp = ontap_rest_request(args, method, path, **kwargs)
    except requests.RequestException as exc:
        return 0, {"message": str(exc)}
    try:
        body = resp.json() if resp.content else {}
    except ValueError:
        body = {"message": resp.text}
    return resp.status_code, body


def wait_for_ontap_job(args, body, description):
    """Many ONTAP REST mutations are asynchronous: a successful call returns
    {"job": {"uuid": ..., "_links": {"self": {"href": "/api/cluster/jobs/..."}}}}
    immediately, and the actual work happens in that job. Polls it (job
    states: queued/running/paused/success/failure) until it settles. A
    response with no "job" key was synchronous (e.g. export-policy create),
    so there's nothing to wait for. Returns (ok, error_message_or_None)."""
    job = (body or {}).get("job")
    if not job:
        return True, None
    href = (job.get("_links") or {}).get("self", {}).get("href") or f"/cluster/jobs/{job.get('uuid')}"
    path = href[4:] if href.startswith("/api/") else href
    print(f"[INFO] {description}: waiting for ONTAP job {job.get('uuid')}...")
    for _ in range(120):
        status, jbody = ontap_rest_json(args, "GET", path)
        if status != 200:
            return False, f"could not poll job status (HTTP {status}): {jbody.get('message', '')}"
        state = jbody.get("state")
        if state == "success":
            return True, None
        if state == "failure":
            err = (jbody.get("error") or {}).get("message") or jbody.get("message") or "job failed"
            return False, err
        time.sleep(2)
    return False, "job did not complete within timeout (240s)"


def rest_step(args, description, method, path, json_body=None, params=None):
    """REST equivalent of run_step(): issues one mutating ONTAP REST call,
    waits for its job to finish if it started one, and returns (success,
    response_body). In --dry-run mode, prints what would be sent instead of
    sending it and simulates success, exactly like run_step()."""
    print(f"[INFO] {description}...")
    if getattr(args, "dry_run", False):
        print(f"[DRY-RUN] Would {method} {path} params={params or {}} body={json_body}")
        return True, {}
    call_params = dict(params or {})
    if method == "POST":
        call_params.setdefault("return_records", "true")
    status, body = ontap_rest_json(args, method, path, params=call_params, json=json_body)
    if status not in (200, 201, 202):
        detail = body.get("message") if isinstance(body, dict) else body
        print(f"[ERROR-DETAIL] HTTP {status}: {detail}", file=sys.stderr)
        return False, body
    ok, err = wait_for_ontap_job(args, body, description)
    if not ok:
        print(f"[ERROR-DETAIL] {err}", file=sys.stderr)
        return False, body
    return True, body


# REST rollback stack: (description, zero-arg callable) pairs, run in
# reverse order on failure - the REST equivalent of ROLLBACK_STEPS/
# push_rollback()/run_rollback(), which replay raw ssh commands instead.
ROLLBACK_STEPS_REST = []


def push_rollback_rest(description, fn):
    ROLLBACK_STEPS_REST.append((description, fn))


def run_rollback_rest(args):
    if not ROLLBACK_STEPS_REST:
        return
    print(f"[WARN] Rolling back {len(ROLLBACK_STEPS_REST)} already-completed step(s) on {args.cluster}...", file=sys.stderr)
    while ROLLBACK_STEPS_REST:
        description, fn = ROLLBACK_STEPS_REST.pop()
        print(f"[WARN] Rollback: {description}", file=sys.stderr)
        try:
            ok = fn()
        except Exception as exc:
            ok = False
            print(f"[WARN] Rollback step raised an error: {exc}", file=sys.stderr)
        if not ok:
            print(f"[WARN] Rollback step itself failed - MANUAL CLEANUP NEEDED on {args.cluster}: {description}", file=sys.stderr)


def get_volume_rest(args, volume_name, fields):
    """Looks up one volume by name+svm and returns its record dict (with
    only the requested fields populated), or None if not found."""
    status, body = ontap_rest_json(
        args, "GET", "/storage/volumes",
        params={"name": volume_name, "svm.name": args.svm_name, "fields": ",".join(fields)},
    )
    if status != 200:
        return None
    records = body.get("records") or []
    return records[0] if records else None


def get_svm_lifs_rest(args):
    status, body = ontap_rest_json(
        args, "GET", "/network/ip/interfaces",
        params={"svm.name": args.svm_name, "fields": "ip.address"},
    )
    if status != 200:
        return []
    return [rec["ip"]["address"] for rec in body.get("records", []) if (rec.get("ip") or {}).get("address")]


def get_svm_lifs(args):
    if getattr(args, "api_mode", "ssh") == "rest":
        return get_svm_lifs_rest(args)
    rows = get_ontap_fields(args.user, args.cluster, f"net interface show -vserver {args.svm_name}", ["address"])
    return [row[0] for row in rows if row[0]]


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


def list_aggregates_rest(args):
    """REST equivalent of "storage aggregate show", using the well-documented
    GET /api/storage/aggregates collection endpoint."""
    status, body = ontap_rest_json(
        args, "GET", "/storage/aggregates",
        params={"fields": "space.block_storage.size,space.block_storage.available,space.block_storage.used,state,volume_count"},
    )
    if status != 200:
        error_exit(f"Could not fetch aggregate list from {args.cluster} (HTTP {status}): {body.get('message', '')}")

    records = body.get("records", [])
    name_width = max([len("Aggregate")] + [len(rec.get("name", "-")) for rec in records]) + 2
    lines = [f"{'Aggregate':<{name_width}}{'Size':>12}{'Available':>12}{'Used':>12}  {'State':<10}{'Vols':>6}"]
    for rec in records:
        block = (rec.get("space") or {}).get("block_storage") or {}
        lines.append(
            f"{rec.get('name', '-'):<{name_width}}"
            f"{format_bytes_human(block.get('size', '-')):>12}"
            f"{format_bytes_human(block.get('available', '-')):>12}"
            f"{format_bytes_human(block.get('used', '-')):>12}"
            f"  {rec.get('state', '-'):<10}{rec.get('volume_count', '-'):>6}"
        )
    return "\n".join(lines) + "\n"


def list_aggregates_ssh(user, cluster):
    """Raw ONTAP tabular output: Aggregate, Size, Available, Used%, State,
    #Vols, Nodes, RAID Status - already includes TB occupancy and volume
    count per aggregate."""
    result = ssh_capture(user, cluster, "storage aggregate show")
    if result.returncode != 0:
        error_exit(f"Could not fetch aggregate list from {cluster}")
    return result.stdout.replace("\r", "")


def list_aggregates(args):
    if getattr(args, "api_mode", "ssh") == "rest":
        return list_aggregates_rest(args)
    return list_aggregates_ssh(args.user, args.cluster)


def select_aggregate_interactively(args):
    print("", file=sys.stderr)
    print(f"No --aggregate given. Current aggregate occupancy on {args.cluster}:", file=sys.stderr)
    print("", file=sys.stderr)
    print(list_aggregates(args), file=sys.stderr)
    print("", file=sys.stderr)
    aggregate = input("Enter the aggregate name to use: ").strip()
    if not aggregate:
        error_exit("No aggregate selected")
    return aggregate


SIZE_WITH_UNIT_RE = re.compile(r"^([0-9]+(\.[0-9]+)?)([A-Za-z]+)$")
SIZE_BARE_RE = re.compile(r"^([0-9]+(\.[0-9]+)?)$")


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


VOLUME_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def validate_inputs(args):
    if args.snap_policy not in VALID_SNAP_POLICIES:
        error_exit(f"snap_policy must be one of: {' '.join(VALID_SNAP_POLICIES)}")

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
    if result.stdout:
        sys.stdout.write(result.stdout)
        if not result.stdout.endswith("\n"):
            sys.stdout.write("\n")
    if result.stderr:
        sys.stderr.write(result.stderr)
        if not result.stderr.endswith("\n"):
            sys.stderr.write("\n")
    return result.returncode == 0


def push_rollback(cmd):
    ROLLBACK_STEPS.append(cmd)


def run_rollback(args):
    if not ROLLBACK_STEPS:
        return
    print(f"[WARN] Rolling back {len(ROLLBACK_STEPS)} already-completed step(s) on {args.cluster}...", file=sys.stderr)
    while ROLLBACK_STEPS:
        step = ROLLBACK_STEPS.pop()
        print(f"[WARN] Rollback: {step}", file=sys.stderr)
        result = ssh_capture(args.user, args.cluster, f"set -confirmations off; {step}")
        if result.returncode != 0:
            print(f"[WARN] Rollback step itself failed - MANUAL CLEANUP NEEDED on {args.cluster}: {step}", file=sys.stderr)


# ── Generic ONTAP field query (showseparator + header-driven parsing) ──────
# ONTAP's column order for "-fields a,b,c" output has proven NOT reliably
# predictable - neither request order nor alphabetical held consistently
# across real tests. Rather than guess a position, this uses
# "set -showseparator :" so ONTAP prints an explicit short-name header row
# first, then reads THAT to find each field's real column index - correct
# no matter what order ONTAP actually used, in a single call.
def get_ontap_fields(user, cluster, cmd, fields):
    """Returns a list of tuples, one per matching data row (there can be
    more than one - e.g. multiple LIFs, multiple export-policy rules),
    values in the same order as `fields`, "" for any field ONTAP didn't
    return. Returns [] if the command matched no rows."""
    remote_cmd = f"set -showseparator :; {cmd} -fields {','.join(fields)}"
    result = ssh_capture(user, cluster, remote_cmd, combine_stderr=True)
    raw = result.stdout.replace("\r", "")

    colidx = {}
    got_header = False
    got_long = False
    rows = []

    for line in raw.split("\n"):
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
    lifs = [ip for ip in all_lifs if not ip.startswith(COHESITY_BACKUP_NETWORK_PREFIX)]
    lifs_str = " ".join(lifs)

    if not all_lifs:
        if getattr(args, "api_mode", "ssh") == "rest":
            print("  Could not determine the NAS server address automatically - check manually:")
            print(f"    GET https://{remote_host(args.cluster)}/api/network/ip/interfaces?svm.name={args.svm_name}&fields=ip.address")
        else:
            print("  Could not determine the NAS server address automatically - check manually:")
            print(f'    ssh -l {args.user} {remote_host(args.cluster)} "net interface show -vserver {args.svm_name} -fields address"')
    elif not lifs:
        print(f"  No client-facing NAS server address available for {args.svm_name} - check the SVM's LIF configuration.")
    else:
        print(f"  NAS server address: {lifs_str}")

    if protocol == "nfs":
        cm = getattr(args, "client_match", None) or ""
        if not cm and policy_for_lookup:
            rows = get_ontap_fields(
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


def execute_ssh_commands(user, host, remote_cmd, dry_run=False):
    """Non-critical/read-only "show" commands after a volume is confirmed
    created - failures there are just warnings, nothing to roll back."""
    if dry_run:
        print(f"[DRY-RUN] Would run informational lookup on {host}: {remote_cmd}")
        return
    result = ssh_capture(user, host, remote_cmd)
    if result.stdout:
        sys.stdout.write(result.stdout)
        if not result.stdout.endswith("\n"):
            sys.stdout.write("\n")
    if result.returncode != 0:
        print(f"[WARN] Non-critical SSH command failed on {host} (volume itself is already created, nothing to roll back)", file=sys.stderr)


def create_nfs_volume(args, size_num, size_unit):
    if args.snap_policy == "none":
        vol_size_calculated = f"{size_num}{size_unit}"
        snapshot_space = 0
    else:
        padded = float(size_num) * 10 / 9
        vol_size_calculated = f"{padded:.2f}{size_unit}"
        snapshot_space = 10

    jct_path = f"/{args.volume_name}"
    tiering_opt = f"-tiering-policy {args.tiering_policy}" if args.tiering_policy else ""

    print(f"[INFO] Creating NFS volume {args.volume_name}...")

    if not run_step(args, f"Creating export policy {args.export_policy}",
                     f"export-policy create -vserver {args.svm_name} -policyname {args.export_policy}"):
        error_exit("Failed to create export policy - nothing was created, aborting.")
    push_rollback(f"export-policy delete -vserver {args.svm_name} -policyname {args.export_policy}")

    if not run_step(
        args, f"Creating export policy rule for {args.client_match}",
        f"export-policy rule create -vserver {args.svm_name} -policyname {args.export_policy} "
        f"-clientmatch {args.client_match} -rorule any -rwrule any -protocol nfs4 -superuser sys",
    ):
        run_rollback(args)
        error_exit("Failed to create export policy rule - rolled back, nothing left over on %s." % args.cluster)

    create_cmd = (
        f"volume create -vserver {args.svm_name} -volume {args.volume_name} -aggregate {args.aggregate} "
        f"-size {vol_size_calculated} -state online -policy {args.export_policy} {tiering_opt} "
        f"-is-space-reporting-logical true -is-space-enforcement-logical true -space-guarantee none "
        f"-snapshot-policy {args.snap_policy} -percent-snapshot-space {snapshot_space} "
        f'-junction-path {jct_path} -comment "{args.comment}"'
    )
    if not run_step(args, f"Creating volume {args.volume_name}", create_cmd):
        run_rollback(args)
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

    print_client_access_info(args, "nfs", jct_path, args.export_policy)


def create_cifs_volume(args, size_num, size_unit):
    padded = float(size_num) * 10 / 9
    vol_size_calculated = f"{padded:.2f}{size_unit}"
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
        f"set -confirmations off; volume offline -vserver {args.svm_name} -volume {args.volume_name}; "
        f"volume delete -vserver {args.svm_name} -volume {args.volume_name}"
    )

    print("[INFO] Waiting for operations to complete...")
    time.sleep(2)

    if not run_step(
        args, f"Mounting volume at {args.junction_path}",
        f"mount -volume {args.volume_name} -vserver {args.svm_name} -junction-path {args.junction_path} -active true",
    ):
        run_rollback(args)
        error_exit(f"Failed to mount volume - rolled back (volume deleted), nothing left over on {args.cluster}.")

    # Home-directory volumes get a default 15GB per-user quota
    if args.volume_name.startswith("home"):
        print("[INFO] Volume name starts with 'home' - applying default 15GB user quota...")

        if not run_step(
            args, "Creating quota rule (15GB per user)",
            f'volume quota policy rule create -vserver {args.svm_name} -policy-name default -volume '
            f'{args.volume_name} -type user -target "" -qtree "" -disk-limit 15GB',
        ):
            run_rollback(args)
            error_exit(f"Failed to create quota rule - rolled back (volume deleted), nothing left over on {args.cluster}.")

        if not run_step(args, "Enabling quota", f"volume quota on -vserver {args.svm_name} -volume {args.volume_name}"):
            run_rollback(args)
            error_exit(f"Failed to enable quota - rolled back (volume deleted), nothing left over on {args.cluster}.")

        time.sleep(5)
        # Quota resize failing is non-fatal - quota is already enabled at
        # this point, just not immediately resized. Not worth a full rollback.
        if not run_step(args, "Resizing quota", f"volume quota resize -vserver {args.svm_name} -volume {args.volume_name}"):
            print("[WARN] Quota resize failed - quota is enabled but may need a manual resize later.", file=sys.stderr)
        print("[INFO] Quota set for home volume.")

    print(f"[INFO] Volume {args.volume_name} created and mounted successfully.")
    print(f"[INFO] Junction-path: {args.junction_path}")

    print_client_access_info(args, "cifs", args.junction_path)


def delete_volume(args):
    print(f"[INFO] Looking up volume {args.volume_name} on {args.svm_name} ({args.cluster})...")
    rows = get_ontap_fields(args.user, args.cluster, f"volume show -vserver {args.svm_name} -volume {args.volume_name}",
                             ["policy", "junction-path"])
    policy_name, jpath = rows[0] if rows else ("", "")

    if not policy_name:
        error_exit(f"Could not find volume '{args.volume_name}' on vserver '{args.svm_name}' - aborting, nothing touched.")

    print(f"[INFO] Found volume '{args.volume_name}' - export-policy: '{policy_name}', junction-path: '{jpath or '-'}'.")

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

    if other_users:
        print(f"[WARN] Export-policy '{policy_name}' is still used by other volume(s) on {args.svm_name} - NOT deleting it:")
        for v in other_users:
            print(f"  - {v}")
        return

    if not run_step(args, f"Deleting export-policy {policy_name}",
                     f"export-policy delete -vserver {args.svm_name} -policyname {policy_name}"):
        print(f"[WARN] Could not delete export-policy '{policy_name}' - may need manual cleanup on {args.cluster}.", file=sys.stderr)
    else:
        print(f"[INFO] Export-policy '{policy_name}' deleted.")


# ── ONTAP REST volume create/delete (--api rest) ────────────────────────────
# Endpoints, required/optional fields, and the async-job pattern below were
# read directly out of the ONTAP REST OpenAPI spec (POST/PATCH/DELETE
# /storage/volumes, /protocols/nfs/export-policies(/rules), /storage/quota/
# rules, /network/ip/interfaces, /cluster/jobs/{uuid}), not guessed - see the
# field-by-field notes in the commit that introduced these functions.

def create_nfs_volume_rest(args, size_num, size_unit):
    if args.snap_policy == "none":
        vol_size_calculated = f"{size_num}{size_unit}"
        snapshot_reserve = 0
    else:
        padded = float(size_num) * 10 / 9
        vol_size_calculated = f"{padded:.2f}{size_unit}"
        snapshot_reserve = 10

    jct_path = f"/{args.volume_name}"

    print(f"[INFO] Creating NFS volume {args.volume_name}...")

    ok, body = rest_step(
        args, f"Creating export policy {args.export_policy}", "POST",
        "/protocols/nfs/export-policies",
        json_body={"name": args.export_policy, "svm": {"name": args.svm_name}},
    )
    if not ok:
        error_exit("Failed to create export policy - nothing was created, aborting.")

    policy_id = None
    if not args.dry_run:
        records = body.get("records") or []
        policy_id = records[0]["id"] if records else None
        if policy_id is None:
            error_exit("Export policy was created but its id was not returned by ONTAP - cannot continue.")

    push_rollback_rest(
        f"delete export-policy {args.export_policy}",
        lambda: rest_step(args, f"Rollback: delete export-policy {args.export_policy}", "DELETE",
                           f"/protocols/nfs/export-policies/{policy_id}")[0],
    )

    rule_path = f"/protocols/nfs/export-policies/{policy_id if policy_id is not None else '<policy-id>'}/rules"
    ok, _ = rest_step(
        args, f"Creating export policy rule for {args.client_match}", "POST", rule_path,
        json_body={
            "clients": [{"match": args.client_match}],
            "ro_rule": ["any"], "rw_rule": ["any"],
            "protocols": ["nfs4"], "superuser": ["sys"],
        },
    )
    if not ok:
        run_rollback_rest(args)
        error_exit(f"Failed to create export policy rule - rolled back, nothing left over on {args.cluster}.")

    volume_body = {
        "name": args.volume_name,
        "svm": {"name": args.svm_name},
        "aggregates": [{"name": args.aggregate}],
        "size": vol_size_calculated,
        "state": "online",
        "comment": args.comment,
        "nas": {"export_policy": {"name": args.export_policy}, "path": jct_path},
        "snapshot_policy": {"name": args.snap_policy},
        "space": {
            "snapshot": {"reserve_percent": snapshot_reserve},
            "logical_space": {"enforcement": True, "reporting": True},
        },
        "guarantee": {"type": "none"},
    }
    ok, _ = rest_step(args, f"Creating volume {args.volume_name}", "POST", "/storage/volumes", json_body=volume_body)
    if not ok:
        run_rollback_rest(args)
        error_exit(f"Failed to create volume - rolled back, nothing left over on {args.cluster}.")

    # movement.tiering_policy is marked modify-only in the ONTAP schema (it
    # can't be set in the create body), so it needs a follow-up PATCH.
    if args.tiering_policy:
        if args.dry_run:
            print(f"[DRY-RUN] Would set tiering policy to {args.tiering_policy} after creation.")
        else:
            vol = get_volume_rest(args, args.volume_name, ["uuid"])
            if vol and vol.get("uuid"):
                rest_step(args, f"Setting tiering policy {args.tiering_policy}", "PATCH",
                          f"/storage/volumes/{vol['uuid']}",
                          json_body={"movement": {"tiering_policy": args.tiering_policy}})
            else:
                print("[WARN] Volume created but could not be looked up afterward to set --tiering-policy - set it manually.", file=sys.stderr)

    print(f"[INFO] Volume {args.volume_name} created successfully.")
    print(f"[INFO] Junction-path: {jct_path}")

    print_client_access_info(args, "nfs", jct_path, args.export_policy)


def create_cifs_volume_rest(args, size_num, size_unit):
    padded = float(size_num) * 10 / 9
    vol_size_calculated = f"{padded:.2f}{size_unit}"

    print(f"[INFO] Creating CIFS volume {args.volume_name}...")

    volume_body = {
        "name": args.volume_name,
        "svm": {"name": args.svm_name},
        "aggregates": [{"name": args.aggregate}],
        "size": vol_size_calculated,
        "state": "online",
        "comment": args.comment,
        "nas": {"security_style": "ntfs"},
        "snapshot_policy": {"name": args.snap_policy},
        "space": {
            "snapshot": {"reserve_percent": 10},
            "logical_space": {"enforcement": True, "reporting": True},
        },
        "guarantee": {"type": "none"},
    }
    ok, _ = rest_step(args, f"Creating volume {args.volume_name}", "POST", "/storage/volumes", json_body=volume_body)
    if not ok:
        error_exit("Failed to create volume - nothing was created, aborting.")

    def _rollback_delete_volume():
        vol = get_volume_rest(args, args.volume_name, ["uuid"])
        if not vol:
            return False
        vol_uuid = vol["uuid"]
        ok1, _ = rest_step(args, f"Rollback: taking volume {args.volume_name} offline", "PATCH",
                            f"/storage/volumes/{vol_uuid}", json_body={"state": "offline"})
        ok2, _ = rest_step(args, f"Rollback: deleting volume {args.volume_name}", "DELETE",
                            f"/storage/volumes/{vol_uuid}")
        return ok1 and ok2

    push_rollback_rest(f"offline+delete volume {args.volume_name}", _rollback_delete_volume)

    volume_uuid = None
    if not args.dry_run:
        vol = get_volume_rest(args, args.volume_name, ["uuid"])
        volume_uuid = vol["uuid"] if vol else None
        if not volume_uuid:
            run_rollback_rest(args)
            error_exit(f"Volume was created but could not be looked up afterward on {args.cluster} - rolled back.")

    if args.tiering_policy:
        if args.dry_run:
            print(f"[DRY-RUN] Would set tiering policy to {args.tiering_policy} after creation.")
        else:
            rest_step(args, f"Setting tiering policy {args.tiering_policy}", "PATCH", f"/storage/volumes/{volume_uuid}",
                      json_body={"movement": {"tiering_policy": args.tiering_policy}})

    print("[INFO] Waiting for operations to complete...")

    mount_path = f"/storage/volumes/{volume_uuid if volume_uuid else '<volume-uuid>'}"
    ok, _ = rest_step(args, f"Mounting volume at {args.junction_path}", "PATCH", mount_path,
                       json_body={"nas": {"path": args.junction_path}})
    if not ok:
        run_rollback_rest(args)
        error_exit(f"Failed to mount volume - rolled back (volume deleted), nothing left over on {args.cluster}.")

    # Home-directory volumes get a default 15GB per-user quota
    if args.volume_name.startswith("home"):
        print("[INFO] Volume name starts with 'home' - applying default 15GB user quota...")

        ok, _ = rest_step(
            args, "Creating quota rule (15GB per user)", "POST", "/storage/quota/rules",
            json_body={
                "svm": {"name": args.svm_name}, "volume": {"name": args.volume_name},
                "type": "user", "users": [{"name": ""}], "qtree": {"name": ""},
                "space": {"hard_limit": 15 * 1024 * 1024 * 1024},
            },
        )
        if not ok:
            run_rollback_rest(args)
            error_exit(f"Failed to create quota rule - rolled back (volume deleted), nothing left over on {args.cluster}.")

        ok, _ = rest_step(args, "Enabling quota", "PATCH", mount_path, json_body={"quota": {"enabled": True}})
        if not ok:
            run_rollback_rest(args)
            error_exit(f"Failed to enable quota - rolled back (volume deleted), nothing left over on {args.cluster}.")
        # ONTAP REST has no separate "quota resize" action - enabling quota
        # (above) triggers the equivalent recalculation as part of its job,
        # unlike the CLI's two distinct "quota on" / "quota resize" steps.
        print("[INFO] Quota set for home volume.")

    print(f"[INFO] Volume {args.volume_name} created and mounted successfully.")
    print(f"[INFO] Junction-path: {args.junction_path}")

    print_client_access_info(args, "cifs", args.junction_path)


def delete_volume_rest(args):
    print(f"[INFO] Looking up volume {args.volume_name} on {args.svm_name} ({args.cluster})...")
    vol = get_volume_rest(args, args.volume_name,
                           ["uuid", "nas.export_policy.name", "nas.export_policy.id", "nas.path"])
    if not vol:
        error_exit(f"Could not find volume '{args.volume_name}' on vserver '{args.svm_name}' - aborting, nothing touched.")

    volume_uuid = vol["uuid"]
    nas = vol.get("nas") or {}
    policy = nas.get("export_policy") or {}
    policy_name = policy.get("name") or ""
    policy_id = policy.get("id")
    jpath = nas.get("path") or "-"

    print(f"[INFO] Found volume '{args.volume_name}' - export-policy: '{policy_name or '-'}', junction-path: '{jpath}'.")

    if args.dry_run:
        print(f"[DRY-RUN] Would prompt to confirm deletion of '{args.volume_name}', then run the steps below.")
    elif not args.assume_yes:
        print("")
        confirm = input(
            f"Delete volume '{args.volume_name}' on {args.svm_name} ({args.cluster})? This is IRREVERSIBLE. Type DELETE to confirm: "
        )
        if confirm != "DELETE":
            print("Aborted. Nothing was touched.")
            sys.exit(0)

    if jpath and jpath != "-":
        ok, _ = rest_step(args, "Unmounting volume", "PATCH", f"/storage/volumes/{volume_uuid}",
                           json_body={"nas": {"path": ""}})
        if not ok:
            error_exit("Failed to unmount volume - aborting before delete, nothing else touched.")

    ok, _ = rest_step(args, "Taking volume offline", "PATCH", f"/storage/volumes/{volume_uuid}",
                       json_body={"state": "offline"})
    if not ok:
        error_exit("Failed to take volume offline - aborting before delete.")

    ok, _ = rest_step(args, "Deleting volume", "DELETE", f"/storage/volumes/{volume_uuid}")
    if not ok:
        error_exit(f"Failed to delete volume. It is now offline but still present - check manually on {args.cluster}.")

    print(f"[INFO] Volume '{args.volume_name}' deleted.")

    if not policy_name or policy_name in ("default", "none"):
        print(f"[INFO] Export-policy '{policy_name or '-'}' is a built-in ONTAP policy or unknown - not touching it.")
        return

    # Safety check: only delete the export-policy if no OTHER volume on
    # this vserver still references it.
    status, body = ontap_rest_json(args, "GET", "/storage/volumes",
                                    params={"svm.name": args.svm_name, "fields": "name,nas.export_policy.name"})
    other_users = []
    if status == 200:
        for rec in body.get("records") or []:
            rec_policy = ((rec.get("nas") or {}).get("export_policy") or {}).get("name")
            if rec_policy == policy_name:
                other_users.append(rec.get("name"))

    if other_users:
        print(f"[WARN] Export-policy '{policy_name}' is still used by other volume(s) on {args.svm_name} - NOT deleting it:")
        for v in other_users:
            print(f"  - {v}")
        return

    ok, _ = rest_step(args, f"Deleting export-policy {policy_name}", "DELETE",
                       f"/protocols/nfs/export-policies/{policy_id}")
    if not ok:
        print(f"[WARN] Could not delete export-policy '{policy_name}' - may need manual cleanup on {args.cluster}.", file=sys.stderr)
    else:
        print(f"[INFO] Export-policy '{policy_name}' deleted.")


# ── Cohesity backup integration ────────────────────────────────────────────
# After a volume is successfully created, add it to the matching Cohesity
# protection job so it isn't left unprotected until someone remembers to do
# it by hand. Failures here are always WARNINGS, never fatal - the volume
# itself is already created and usable; Cohesity registration can be redone
# manually if something goes wrong.

def map_cohesity_cluster(cluster):
    """Returns (cohesity_cluster, suffix) or None if unmapped."""
    return COHESITY_CLUSTER_MAP.get(cluster)


def select_backup_tier_interactively():
    print("", file=sys.stderr)
    print("No --backup-tier given. Choose the Cohesity retention tier for this volume:", file=sys.stderr)
    print("  short - ShortTerm", file=sys.stderr)
    print("  mid   - MidTerm", file=sys.stderr)
    print("  long  - LongTerm", file=sys.stderr)
    print("  none  - skip Cohesity protection for this volume", file=sys.stderr)
    return input("Tier [short/mid/long/none]: ")


def validate_backup_tier(backup_tier):
    if backup_tier not in ("short", "mid", "long", "none"):
        error_exit(f"--backup-tier must be one of: short, mid, long, none (got '{backup_tier}')")


def get_cohesity_apikey(args, cohesity_cluster):
    """Prompts for the Cohesity API key if not given via --cohesity-apikey
    or $COHESITY_APIKEY. Input is hidden, same style as other secret
    prompts."""
    if getattr(args, "cohesity_apikey", None):
        return args.cohesity_apikey
    env_key = os.environ.get("COHESITY_APIKEY")
    if env_key:
        return env_key
    apikey = getpass.getpass(f"Cohesity API key for {cohesity_cluster}: ")
    if not apikey:
        error_exit("No Cohesity API key provided")
    return apikey


def cohesity_request(method, url, apikey, **kwargs):
    headers = kwargs.pop("headers", {})
    headers["apiKey"] = apikey
    return requests.request(method, url, headers=headers, verify=False, timeout=30, **kwargs)


def cohesity_json(method, url, apikey, **kwargs):
    try:
        resp = cohesity_request(method, url, apikey, **kwargs)
        return resp.json()
    except (requests.RequestException, ValueError):
        return None


def find_cohesity_source_id(cohesity_cluster, apikey, cluster, svm_name, volume_name):
    """Walks the registered kNetapp source tree and returns the Cohesity
    source id for the volume just created (matched on cluster + vserver +
    volume). Field is protectionSource.netappProtectionSource.type
    (kCluster/kVserver/kVolume) - NOT protectionSource.netapp.type, which
    looks similar but is always empty and silently matches nothing."""
    sources_raw = cohesity_json("GET", f"https://{cohesity_cluster}/irisservices/api/v1/public/protectionSources?environments=kNetapp", apikey)
    if not isinstance(sources_raw, list):
        return None

    def walk_node(node, cluster_ctx, vserver_ctx):
        results = []
        ps = node.get("protectionSource", {}) or {}
        netapp_type = (ps.get("netappProtectionSource") or {}).get("type", "")
        name = ps.get("name", "")
        cctx = name if netapp_type == "kCluster" else cluster_ctx
        vctx = name if netapp_type == "kVserver" else vserver_ctx
        if netapp_type == "kVolume":
            results.append({"cluster": cctx, "vserver": vctx, "volume": name, "id": ps.get("id")})
        for child in node.get("nodes") or []:
            results.extend(walk_node(child, cctx, vctx))
        return results

    volume_index = []
    for node in sources_raw:
        volume_index.extend(walk_node(node, None, None))

    for entry in volume_index:
        if entry["cluster"] == cluster and entry["vserver"] == svm_name and entry["volume"] == volume_name:
            return entry["id"]
    return None


def refresh_cohesity_netapp_source(cohesity_cluster, apikey, cluster):
    """Equivalent of "edit + save" on a registered source in the Cohesity
    GUI - forces re-discovery so a volume created seconds ago has a chance
    to show up in the source tree without waiting for the next scheduled
    scan."""
    sources_raw = cohesity_json("GET", f"https://{cohesity_cluster}/irisservices/api/v1/public/protectionSources?environments=kNetapp", apikey)
    cluster_source_id = None
    if isinstance(sources_raw, list):
        for node in sources_raw:
            ps = node.get("protectionSource", {}) or {}
            netapp_type = (ps.get("netappProtectionSource") or {}).get("type", "")
            if netapp_type == "kCluster" and ps.get("name") == cluster:
                cluster_source_id = ps.get("id")
                break

    if cluster_source_id is None:
        print(f"[WARN] Could not find registered NetApp cluster source '{cluster}' on {cohesity_cluster} to refresh - continuing without refresh.", file=sys.stderr)
        return

    print(f"[INFO] Refreshing Cohesity source for '{cluster}' (id {cluster_source_id})...")
    try:
        resp = cohesity_request(
            "POST",
            f"https://{cohesity_cluster}/irisservices/api/v1/public/protectionSources/refresh?id={cluster_source_id}",
            apikey,
        )
        http_code = resp.status_code
        resp_body = resp.text
    except requests.RequestException as exc:
        http_code = None
        resp_body = str(exc)

    if http_code in (200, 204):
        print(f"[INFO] Refresh accepted by Cohesity (HTTP {http_code}).")
    else:
        try:
            err = json.loads(resp_body).get("message") or json.loads(resp_body).get("errorCode") or "unknown error"
        except (ValueError, AttributeError):
            err = resp_body or "empty response"
        print(f"[WARN] Cohesity source refresh failed (HTTP {http_code}) - {err}", file=sys.stderr)
        print("[WARN] The volume-discovery poll below will likely time out - refresh the source manually in the Cohesity UI (edit + save) if it does.", file=sys.stderr)


def protect_volume_in_cohesity(args):
    """Adds the just-created volume to its matching Cohesity protection
    job. Never touches the volume itself - every failure path here is a
    warning, since by the time this runs the volume already exists."""
    print("")

    if getattr(args, "backup_tier", None) == "none":
        print(f"[INFO] --backup-tier none - skipping Cohesity protection for '{args.volume_name}'.")
        return

    print(f"[INFO] Registering {args.volume_name} for Cohesity protection...")

    dry_run = getattr(args, "dry_run", False)

    if not dry_run and not HAVE_REQUESTS:
        print("[WARN] Python 'requests' package not found - skipping Cohesity protection. Add the volume manually (pip install requests).", file=sys.stderr)
        return

    cohesity_cluster = None
    cohesity_suffix = None
    mapped = map_cohesity_cluster(args.cluster)
    if mapped:
        cohesity_cluster, cohesity_suffix = mapped
    if not mapped:
        if not getattr(args, "cohesity_cluster_override", None) or not getattr(args, "cohesity_job_override", None):
            print(f"[WARN] No known Cohesity mapping for NetApp cluster '{args.cluster}' (known: damascus-3, damascus-4, jericho-1, jericho-2).", file=sys.stderr)
            print("[WARN] Pass both --cohesity-cluster and --cohesity-job to protect this volume, or add it manually later. Skipping.", file=sys.stderr)
            return
    if getattr(args, "cohesity_cluster_override", None):
        cohesity_cluster = args.cohesity_cluster_override

    if getattr(args, "cohesity_job_override", None):
        job_name = args.cohesity_job_override
    else:
        backup_tier = getattr(args, "backup_tier", None)
        if not backup_tier:
            backup_tier = select_backup_tier_interactively()
        validate_backup_tier(backup_tier)
        if backup_tier == "none":
            print(f"[INFO] --backup-tier none - skipping Cohesity protection for '{args.volume_name}'.")
            return
        tier_word = {"short": "Short", "mid": "Mid", "long": "Long"}[backup_tier]
        job_name = f"{args.cluster.capitalize()}-{tier_word}Term-{cohesity_suffix}"

    if dry_run:
        print(f"[DRY-RUN] Would register '{args.volume_name}' with Cohesity job '{job_name}' on cluster '{cohesity_cluster}' (no API calls made, no API key needed).")
        return

    apikey = get_cohesity_apikey(args, cohesity_cluster)

    print(f"[INFO] Cohesity cluster: {cohesity_cluster} / job: {job_name}")

    jobs_raw = cohesity_json("GET", f"https://{cohesity_cluster}/irisservices/api/v1/public/protectionJobs?isDeleted=false&isActive=true", apikey)
    if not isinstance(jobs_raw, list):
        print(f"[WARN] Could not fetch protection jobs from {cohesity_cluster} - skipping Cohesity protection, add manually.", file=sys.stderr)
        return

    jobs = [j for j in jobs_raw if j.get("name") == job_name]
    if not jobs:
        print(f"[WARN] No Cohesity protection job named '{job_name}' found on {cohesity_cluster} - skipping. Create the job first, or protect manually.", file=sys.stderr)
        return
    job_id = jobs[0]["id"]

    job_detail = cohesity_json("GET", f"https://{cohesity_cluster}/irisservices/api/v1/public/protectionJobs/{job_id}", apikey)
    if not isinstance(job_detail, dict):
        print(f"[WARN] Could not fetch job detail for '{job_name}' - skipping.", file=sys.stderr)
        return

    # sourceSpecialParameters holds protocol-specific config and is only
    # needed for some volumes (notably CIFS/SMB). A job with none at all is
    # normal and common for NFS-only jobs - so an empty template here is
    # NOT an error, just means "add via sourceIds only" below instead of
    # cloning a per-volume config.
    special_params = job_detail.get("sourceSpecialParameters") or []
    template_param = special_params[0] if special_params else None

    refresh_cohesity_netapp_source(cohesity_cluster, apikey, args.cluster)

    source_id = None
    max_attempts = 12
    for attempt in range(1, max_attempts + 1):
        source_id = find_cohesity_source_id(cohesity_cluster, apikey, args.cluster, args.svm_name, args.volume_name)
        if source_id is not None:
            break
        print(f"[INFO] Volume not yet discovered in Cohesity (attempt {attempt}/{max_attempts}) - waiting 5s...")
        time.sleep(5)

    if source_id is None:
        print(f"[WARN] Volume '{args.volume_name}' was not discovered in Cohesity's source tree after {max_attempts * 5}s - it was NOT added to '{job_name}'.", file=sys.stderr)
        print("[WARN] The volume itself was created successfully. Add it to Cohesity protection manually once it is discovered.", file=sys.stderr)
        return

    existing_source_ids = job_detail.get("sourceIds") or []
    if source_id in existing_source_ids:
        print(f"[INFO] Volume '{args.volume_name}' is already protected by '{job_name}'.")
        return

    if not args.assume_yes:
        print("")
        confirm = input(f"Add '{args.volume_name}' to Cohesity job '{job_name}' on {cohesity_cluster}? [y/N] ")
        if confirm not in ("y", "Y"):
            print(f"[INFO] Skipped Cohesity protection for '{args.volume_name}' (not confirmed). Volume itself is unaffected.")
            return

    updated_source_ids = sorted(set(existing_source_ids + [source_id]))
    updated_job = dict(job_detail)
    updated_job["sourceIds"] = updated_source_ids
    if template_param is not None:
        new_param = dict(template_param)
        new_param["sourceId"] = source_id
        updated_job["sourceSpecialParameters"] = special_params + [new_param]

    response = cohesity_json("PUT", f"https://{cohesity_cluster}/irisservices/api/v1/public/protectionJobs/{job_id}", apikey,
                              headers={"Content-Type": "application/json"}, data=json.dumps(updated_job))

    if isinstance(response, dict) and response.get("id"):
        print(f"[INFO] Volume '{args.volume_name}' added to Cohesity protection job '{job_name}' on {cohesity_cluster}.")
    else:
        err = "unknown error"
        if isinstance(response, dict):
            err = response.get("message") or response.get("errorCode") or "unknown error"
        print(f"[WARN] Failed to add '{args.volume_name}' to Cohesity job '{job_name}' - {err}", file=sys.stderr)
        print("[WARN] The volume itself was created successfully. Protect it manually.", file=sys.stderr)


# ── Read-only status check ──────────────────────────────────────────────────
def check_volume_status(args):
    """Reports ONTAP volume state/mount and Cohesity discovery/protection
    status. Makes no changes anywhere - safe to run any time, as many times
    as needed."""
    print("============================================")
    print(f" Status check: {args.volume_name} on {args.svm_name} ({args.cluster})")
    print("============================================")

    print("")
    print("[ONTAP]")
    rows = get_ontap_fields(
        args.user, args.cluster, f"volume show -vserver {args.svm_name} -volume {args.volume_name}",
        ["policy", "junction-path", "state", "size", "used", "security-style"],
    )
    policy_name, jpath, state, size, used, sec_style = rows[0] if rows else ("", "", "", "", "", "")

    if not policy_name and not jpath and not state:
        print(f"  Volume '{args.volume_name}' NOT found on '{args.svm_name}' ({args.cluster}) - or ssh/permission issue.")
    else:
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
            result = ssh_capture(
                args.user, args.cluster,
                f"export-policy rule show -vserver {args.svm_name} -policyname {policy_name} -fields clientmatch,protocol",
                combine_stderr=True,
            )
            for line in result.stdout.replace("\r", "").splitlines():
                print(f"    {line}")

        # Protocol for the access-info block below: use --type if given,
        # else infer from security-style (set explicitly to ntfs by
        # create_cifs_volume; NFS volumes keep the SVM's unix/mixed default).
        # This is a best-effort guess when --type isn't passed to --check -
        # pass --type explicitly to be sure.
        protocol_guess = getattr(args, "volume_type", None) or ""
        if not protocol_guess:
            if sec_style == "ntfs":
                protocol_guess = "cifs"
            elif sec_style in ("unix", "mixed"):
                protocol_guess = "nfs"
        print_client_access_info(args, protocol_guess, jpath or "-", policy_name)

    print("")
    print("[Cohesity]")

    if not HAVE_REQUESTS:
        print("  Python 'requests' package not found - cannot check Cohesity status.")
        return

    cohesity_cluster = None
    mapped = map_cohesity_cluster(args.cluster)
    if mapped:
        cohesity_cluster = mapped[0]
    if not mapped and not getattr(args, "cohesity_cluster_override", None):
        print(f"  No known Cohesity mapping for cluster '{args.cluster}' - pass --cohesity-cluster to check. Skipping.")
        return
    if getattr(args, "cohesity_cluster_override", None):
        cohesity_cluster = args.cohesity_cluster_override

    apikey = get_cohesity_apikey(args, cohesity_cluster)

    source_id = find_cohesity_source_id(cohesity_cluster, apikey, args.cluster, args.svm_name, args.volume_name)
    if source_id is None:
        print(f"  Not discovered in Cohesity's source tree on {cohesity_cluster}.")
        print("  (Discovery can lag a real creation by a few minutes, or the source may need a refresh - see --backup-only.)")
        return
    print(f"  Discovered on {cohesity_cluster} (source id: {source_id}).")

    jobs_raw = cohesity_json("GET", f"https://{cohesity_cluster}/irisservices/api/v1/public/protectionJobs?isDeleted=false&isActive=true", apikey)
    if not isinstance(jobs_raw, list):
        print(f"  Could not fetch protection jobs from {cohesity_cluster} to check protection status.")
        return

    job_ids = [j["id"] for j in jobs_raw if j.get("environment") == "kNetapp"]
    matches = []
    for jid in job_ids:
        detail = cohesity_json("GET", f"https://{cohesity_cluster}/irisservices/api/v1/public/protectionJobs/{jid}", apikey)
        if isinstance(detail, dict) and source_id in (detail.get("sourceIds") or []):
            matches.append(detail.get("name"))

    if matches:
        print("  Protected by:")
        for name in matches:
            print(f"    - {name}")
    else:
        print(f"  NOT currently protected by any active Cohesity job on {cohesity_cluster}.")


def build_parser():
    parser = argparse.ArgumentParser(
        prog="netapp_volume_create.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=EPILOG,
        add_help=False,
    )
    parser.add_argument("--type", dest="volume_type")
    parser.add_argument("--user")
    parser.add_argument("--cluster")
    parser.add_argument("--svm", dest="svm_name")
    parser.add_argument("--volume", dest="volume_name")
    parser.add_argument("--size", dest="vol_size")
    parser.add_argument("--aggregate")
    parser.add_argument("--snap-policy", dest="snap_policy")
    parser.add_argument("--tiering-policy", dest="tiering_policy")
    parser.add_argument("--export-policy", dest="export_policy")
    parser.add_argument("--client-match", dest="client_match")
    parser.add_argument("--junction-path", dest="junction_path")
    parser.add_argument("--comment")
    parser.add_argument("--backup-tier", dest="backup_tier")
    parser.add_argument("--cohesity-apikey", dest="cohesity_apikey")
    parser.add_argument("--cohesity-cluster", dest="cohesity_cluster_override")
    parser.add_argument("--cohesity-job", dest="cohesity_job_override")
    parser.add_argument("--no-backup", dest="no_backup", action="store_true")
    parser.add_argument("--backup-only", dest="backup_only", action="store_true")
    parser.add_argument("--check", dest="check_mode", action="store_true")
    parser.add_argument("--list-aggregates", dest="list_aggregates_only", action="store_true")
    parser.add_argument("--delete", dest="delete_mode", action="store_true")
    parser.add_argument("--yes", dest="assume_yes", action="store_true")
    parser.add_argument("--dry-run", dest="dry_run", action="store_true")
    parser.add_argument("--api", dest="api_mode", default="ssh")
    parser.add_argument("--cert-file", dest="ontap_cert_file")
    parser.add_argument("--key-file", dest="ontap_key_file")
    parser.add_argument("--password", dest="ontap_password")
    parser.add_argument("--ca-bundle", dest="ontap_ca_bundle")
    parser.add_argument("--insecure-ontap", dest="ontap_insecure", action="store_true")
    parser.add_argument("-h", "--help", action="store_true", dest="show_help")
    parser.add_argument("--version", action="store_true")
    return parser


def require_args(args, names):
    for name in names:
        if not getattr(args, name, None):
            error_exit(f"Missing required argument: --{name.replace('_', '-')}")


def main(argv=None):
    parser = build_parser()
    try:
        args, unknown = parser.parse_known_args(argv)
    except SystemExit:
        raise
    if unknown:
        error_exit(f"Unknown option: {unknown[0]}")

    if args.show_help:
        parser.print_help()
        sys.exit(0)

    if args.version:
        print(f"netapp_volume_create.py version {SCRIPT_VERSION}")
        sys.exit(0)

    # Defaults for optional parameters
    if not args.comment:
        args.comment = "Created by netapp_volume script"
    if not args.export_policy:
        args.export_policy = args.volume_name

    if args.api_mode not in ("ssh", "rest"):
        error_exit(f"--api must be ssh or rest (got '{args.api_mode}')")

    if args.dry_run:
        print("[INFO] --dry-run: no ONTAP or Cohesity changes will be made. Commands that would run are printed with a [DRY-RUN] prefix.")

    # --list-aggregates: just show occupancy and exit, no volume created
    if args.list_aggregates_only:
        require_args(args, ["user", "cluster"])
        print(list_aggregates(args), end="")
        sys.exit(0)

    # --delete: remove a volume (and its export-policy, if safe) and exit
    if args.delete_mode:
        require_args(args, ["user", "cluster", "svm_name", "volume_name"])
        if args.api_mode == "rest":
            delete_volume_rest(args)
        else:
            delete_volume(args)
        sys.exit(0)

    # --backup-only: register an ALREADY-EXISTING volume with Cohesity and
    # exit - no ONTAP call is made at all (--api is irrelevant here). This
    # is the recovery path if volume creation succeeded but the Cohesity
    # step failed/was skipped (bad --cohesity-apikey, job not found yet,
    # etc.) - re-running the full create command in that situation just
    # fails on "export-policy already exists". --user is intentionally not
    # required here since nothing touches ONTAP in this mode.
    if args.backup_only:
        require_args(args, ["cluster", "svm_name", "volume_name"])
        if args.no_backup:
            error_exit("--backup-only and --no-backup are mutually exclusive")
        protect_volume_in_cohesity(args)
        sys.exit(0)

    # --check: read-only status report (ONTAP + Cohesity), no changes made
    if args.check_mode:
        require_args(args, ["user", "cluster", "svm_name", "volume_name"])
        if args.api_mode == "rest":
            error_exit("--api rest is not yet implemented for --check; use --api ssh (the default) for --check.")
        check_volume_status(args)
        sys.exit(0)

    # Validate required arguments (aggregate is NOT required here - if
    # missing, we prompt interactively below instead of failing)
    require_args(args, ["volume_type", "user", "cluster", "svm_name", "volume_name", "vol_size", "snap_policy"])

    # Parse --size into size_num / size_unit (accepts "100" or "100GB")
    size_num, size_unit = parse_size(args.vol_size)

    # If no aggregate given, show occupancy and ask which one to use
    if not args.aggregate:
        args.aggregate = select_aggregate_interactively(args)

    validate_inputs(args)

    if args.api_mode == "rest":
        if args.volume_type == "nfs":
            create_nfs_volume_rest(args, size_num, size_unit)
        elif args.volume_type == "cifs":
            create_cifs_volume_rest(args, size_num, size_unit)
        else:
            error_exit(f"--type must be nfs or cifs (got '{args.volume_type}')")
    elif args.volume_type == "nfs":
        create_nfs_volume(args, size_num, size_unit)
    elif args.volume_type == "cifs":
        create_cifs_volume(args, size_num, size_unit)
    else:
        error_exit(f"--type must be nfs or cifs (got '{args.volume_type}')")

    print("[INFO] Volume creation completed successfully.")

    # Register the new volume with Cohesity, unless explicitly skipped.
    # Never fatal - the volume above is already created regardless of what
    # happens here.
    if not args.no_backup:
        protect_volume_in_cohesity(args)


if __name__ == "__main__":
    main()
