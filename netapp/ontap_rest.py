"""ONTAP over its REST API (--api rest).

Endpoints, required/optional fields, and the async-job pattern below were
read directly out of the ONTAP REST OpenAPI spec (POST/PATCH/DELETE
/storage/volumes, /protocols/nfs/export-policies(/rules), /storage/quota/
rules, /network/ip/interfaces, /cluster/jobs/{uuid}), not guessed.

Authentication (in this order):
  1. Client certificate, if configured: --cert-file/--key-file, or
     $ONTAP_CERT_FILE/$ONTAP_KEY_FILE. Requires the cert already installed
     in ONTAP and mapped to a user via
     "security login create -authmethod cert -application http ...".
     See: https://docs.netapp.com/us-en/ontap-technical-reports/ontap-security-hardening/set-up-certificate-based-api-access.html
  2. HTTP basic auth otherwise, using --user plus --password, or
     $ONTAP_PASSWORD, or (if neither is given) a hidden interactive prompt
     - the same fallback chain used for the Cohesity API key.
"""

import getpass
import os
import sys
import time

from . import rollback
from .util import (HAVE_REQUESTS, confirm_volume_delete, error_exit, format_bytes_human, remote_host,
                   report_export_policy_deleted, report_export_policy_still_used, requests)
from .validation import nfs_size_and_snapshot_reserve, padded_size


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


def ontap_tls_verify(args):
    """requests' `verify` for ONTAP REST: verified by default (against the
    system CA store, or --ca-bundle / $ONTAP_CA_BUNDLE), False only with an
    explicit --insecure-ontap. A CA bundle path that doesn't exist is
    rejected here with a clear message - requests would otherwise raise a
    bare OSError (not a RequestException) and crash with a traceback."""
    if getattr(args, "ontap_insecure", False):
        return False
    ca_bundle = getattr(args, "ontap_ca_bundle", None) or os.environ.get("ONTAP_CA_BUNDLE")
    if ca_bundle and not os.path.isfile(ca_bundle):
        error_exit(f"ONTAP CA bundle '{ca_bundle}' does not exist")
    return ca_bundle or True


def ontap_rest_request(args, method, path, **kwargs):
    """Issues one raw ONTAP REST call against --cluster and returns the
    requests.Response. TLS verification: see ontap_tls_verify(). Raises requests.RequestException on a connection-level
    failure (DNS/connect/timeout/TLS) - callers use ontap_rest_json(), which
    catches that the same way ssh_capture() lets a failed ssh command return
    a non-zero exit code instead of crashing the script."""
    if not HAVE_REQUESTS:
        error_exit("Python 'requests' package not found - required for --api rest (pip install requests)")

    auth_kwargs = resolve_ontap_auth(args)
    verify = ontap_tls_verify(args)

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
    """REST equivalent of ontap_ssh.run_step(): issues one mutating ONTAP
    REST call, waits for its job to finish if it started one, and returns
    (success, response_body). In --dry-run mode, prints what would be sent
    instead of sending it and simulates success, exactly like run_step()."""
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


def get_svm_lifs(args):
    status, body = ontap_rest_json(
        args, "GET", "/network/ip/interfaces",
        params={"svm.name": args.svm_name, "fields": "ip.address"},
    )
    if status != 200:
        return []
    return [rec["ip"]["address"] for rec in body.get("records", []) if (rec.get("ip") or {}).get("address")]


def list_aggregates(args):
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


def _set_tiering_policy(args, volume_uuid):
    rest_step(args, f"Setting tiering policy {args.tiering_policy}", "PATCH", f"/storage/volumes/{volume_uuid}",
              json_body={"movement": {"tiering_policy": args.tiering_policy}})


def _volume_body(args, size, snapshot_reserve, nas):
    return {
        "name": args.volume_name,
        "svm": {"name": args.svm_name},
        "aggregates": [{"name": args.aggregate}],
        "size": size,
        "state": "online",
        "comment": args.comment,
        "nas": nas,
        "snapshot_policy": {"name": args.snap_policy},
        "space": {
            "snapshot": {"reserve_percent": snapshot_reserve},
            "logical_space": {"enforcement": True, "reporting": True},
        },
        "guarantee": {"type": "none"},
    }


def create_nfs_volume(args, size_num, size_unit):
    """Returns the volume's junction-path."""
    vol_size_calculated, snapshot_reserve = nfs_size_and_snapshot_reserve(args.snap_policy, size_num, size_unit)
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

    rollback.push(
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
        rollback.run(args)
        error_exit(f"Failed to create export policy rule - rolled back, nothing left over on {args.cluster}.")

    volume_body = _volume_body(args, vol_size_calculated, snapshot_reserve,
                               {"export_policy": {"name": args.export_policy}, "path": jct_path})
    ok, _ = rest_step(args, f"Creating volume {args.volume_name}", "POST", "/storage/volumes", json_body=volume_body)
    if not ok:
        rollback.run(args)
        error_exit(f"Failed to create volume - rolled back, nothing left over on {args.cluster}.")

    # movement.tiering_policy is marked modify-only in the ONTAP schema (it
    # can't be set in the create body), so it needs a follow-up PATCH.
    if args.tiering_policy:
        if args.dry_run:
            print(f"[DRY-RUN] Would set tiering policy to {args.tiering_policy} after creation.")
        else:
            vol = get_volume_rest(args, args.volume_name, ["uuid"])
            if vol and vol.get("uuid"):
                _set_tiering_policy(args, vol["uuid"])
            else:
                print("[WARN] Volume created but could not be looked up afterward to set --tiering-policy - set it manually.", file=sys.stderr)

    print(f"[INFO] Volume {args.volume_name} created successfully.")
    print(f"[INFO] Junction-path: {jct_path}")
    return jct_path


def create_cifs_volume(args, size_num, size_unit):
    """Returns the volume's junction-path."""
    vol_size_calculated = padded_size(size_num, size_unit)

    print(f"[INFO] Creating CIFS volume {args.volume_name}...")

    volume_body = _volume_body(args, vol_size_calculated, 10, {"security_style": "ntfs"})
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

    rollback.push(f"offline+delete volume {args.volume_name}", _rollback_delete_volume)

    volume_uuid = None
    if not args.dry_run:
        vol = get_volume_rest(args, args.volume_name, ["uuid"])
        volume_uuid = vol["uuid"] if vol else None
        if not volume_uuid:
            rollback.run(args)
            error_exit(f"Volume was created but could not be looked up afterward on {args.cluster} - rolled back.")

    if args.tiering_policy:
        if args.dry_run:
            print(f"[DRY-RUN] Would set tiering policy to {args.tiering_policy} after creation.")
        else:
            _set_tiering_policy(args, volume_uuid)

    print("[INFO] Waiting for operations to complete...")

    mount_path = f"/storage/volumes/{volume_uuid if volume_uuid else '<volume-uuid>'}"
    ok, _ = rest_step(args, f"Mounting volume at {args.junction_path}", "PATCH", mount_path,
                      json_body={"nas": {"path": args.junction_path}})
    if not ok:
        rollback.run(args)
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
            rollback.run(args)
            error_exit(f"Failed to create quota rule - rolled back (volume deleted), nothing left over on {args.cluster}.")

        ok, _ = rest_step(args, "Enabling quota", "PATCH", mount_path, json_body={"quota": {"enabled": True}})
        if not ok:
            rollback.run(args)
            error_exit(f"Failed to enable quota - rolled back (volume deleted), nothing left over on {args.cluster}.")
        # ONTAP REST has no separate "quota resize" action - enabling quota
        # (above) triggers the equivalent recalculation as part of its job,
        # unlike the CLI's two distinct "quota on" / "quota resize" steps.
        print("[INFO] Quota set for home volume.")

    print(f"[INFO] Volume {args.volume_name} created and mounted successfully.")
    print(f"[INFO] Junction-path: {args.junction_path}")
    return args.junction_path


def delete_volume(args):
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

    confirm_volume_delete(args)

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
    if report_export_policy_still_used(args, policy_name, other_users):
        return

    ok, _ = rest_step(args, f"Deleting export-policy {policy_name}", "DELETE",
                      f"/protocols/nfs/export-policies/{policy_id}")
    report_export_policy_deleted(args, policy_name, ok)
