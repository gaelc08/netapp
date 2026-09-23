"""Cohesity backup integration.

After a volume is successfully created, add it to the matching Cohesity
protection job so it isn't left unprotected until someone remembers to do
it by hand; after a delete, drop the stale registration. Failures here are
always WARNINGS, never fatal - by the time any of this runs, the ONTAP side
is already done; Cohesity registration can be redone manually.
"""

import getpass
import json
import os
import sys
import time

from .constants import BACKUP_TIERS, COHESITY_CLUSTER_MAP
from .util import HAVE_REQUESTS, error_exit, requests


class CohesityClient:
    """Thin wrapper over the Cohesity v1 public API of one cluster. Read
    helpers return None (never raise) on any connection or decoding error,
    so callers can degrade to a warning."""

    def __init__(self, cluster, apikey, verify=False):
        self.cluster = cluster
        self.apikey = apikey
        self.verify = verify
        self.base_url = f"https://{cluster}/irisservices/api/v1/public"

    def request(self, method, path, **kwargs):
        headers = kwargs.pop("headers", {})
        headers["apiKey"] = self.apikey
        return requests.request(method, f"{self.base_url}{path}", headers=headers, verify=self.verify, timeout=30, **kwargs)

    def json(self, method, path, **kwargs):
        try:
            return self.request(method, path, **kwargs).json()
        except (requests.RequestException, ValueError):
            return None

    def netapp_sources(self):
        return self.json("GET", "/protectionSources?environments=kNetapp")

    def active_jobs(self):
        return self.json("GET", "/protectionJobs?isDeleted=false&isActive=true")

    def job(self, job_id):
        return self.json("GET", f"/protectionJobs/{job_id}")

    def update_job(self, job_id, job):
        return self.json("PUT", f"/protectionJobs/{job_id}",
                         headers={"Content-Type": "application/json"}, data=json.dumps(job))

    def find_volume_source_id(self, cluster, svm_name, volume_name):
        """Walks the registered kNetapp source tree and returns the Cohesity
        source id for the volume (matched on cluster + vserver + volume).
        Field is protectionSource.netappProtectionSource.type
        (kCluster/kVserver/kVolume) - NOT protectionSource.netapp.type, which
        looks similar but is always empty and silently matches nothing."""
        sources_raw = self.netapp_sources()
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

    def refresh_netapp_source(self, cluster):
        """Equivalent of "edit + save" on a registered source in the Cohesity
        GUI - forces re-discovery so a volume created seconds ago has a chance
        to show up in the source tree without waiting for the next scheduled
        scan."""
        sources_raw = self.netapp_sources()
        cluster_source_id = None
        if isinstance(sources_raw, list):
            for node in sources_raw:
                ps = node.get("protectionSource", {}) or {}
                netapp_type = (ps.get("netappProtectionSource") or {}).get("type", "")
                if netapp_type == "kCluster" and ps.get("name") == cluster:
                    cluster_source_id = ps.get("id")
                    break

        if cluster_source_id is None:
            print(f"[WARN] Could not find registered NetApp cluster source '{cluster}' on {self.cluster} to refresh - continuing without refresh.", file=sys.stderr)
            return

        print(f"[INFO] Refreshing Cohesity source for '{cluster}' (id {cluster_source_id})...")
        try:
            resp = self.request("POST", f"/protectionSources/refresh/{cluster_source_id}")
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


def _error_message(response):
    if isinstance(response, dict):
        return response.get("message") or response.get("errorCode") or "unknown error"
    return "unknown error"


def map_cohesity_cluster(cluster):
    """Returns (cohesity_cluster, suffix) or None if unmapped."""
    return COHESITY_CLUSTER_MAP.get(cluster)


def resolve_cohesity_cluster(args):
    """--cohesity-cluster if given, else the cluster mapped from --cluster,
    else None."""
    override = getattr(args, "cohesity_cluster_override", None)
    if override:
        return override
    mapped = map_cohesity_cluster(args.cluster)
    return mapped[0] if mapped else None


def select_backup_tier_interactively():
    print("", file=sys.stderr)
    print("No --backup-tier given. Choose the Cohesity retention tier for this volume:", file=sys.stderr)
    print("  short - ShortTerm", file=sys.stderr)
    print("  mid   - MidTerm", file=sys.stderr)
    print("  long  - LongTerm", file=sys.stderr)
    print("  none  - skip Cohesity protection for this volume", file=sys.stderr)
    return input("Tier [short/mid/long/none]: ")


def validate_backup_tier(backup_tier):
    if backup_tier not in BACKUP_TIERS:
        error_exit(f"--backup-tier must be one of: {', '.join(BACKUP_TIERS)} (got '{backup_tier}')")


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


def connect(args, cohesity_cluster):
    return CohesityClient(cohesity_cluster, get_cohesity_apikey(args, cohesity_cluster))


def iter_netapp_job_details(client, jobs_raw):
    """Yields the full detail dict of every kNetapp job in jobs_raw
    (skipping any whose detail can't be fetched), one GET at a time."""
    for jid in [j["id"] for j in jobs_raw if j.get("environment") == "kNetapp"]:
        detail = client.job(jid)
        if isinstance(detail, dict):
            yield jid, detail


def protect_volume(args):
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
    elif not getattr(args, "cohesity_cluster_override", None) or not getattr(args, "cohesity_job_override", None):
        print(f"[WARN] No known Cohesity mapping for NetApp cluster '{args.cluster}' (known: {', '.join(sorted(COHESITY_CLUSTER_MAP))}).", file=sys.stderr)
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

    client = connect(args, cohesity_cluster)

    print(f"[INFO] Cohesity cluster: {cohesity_cluster} / job: {job_name}")

    jobs_raw = client.active_jobs()
    if not isinstance(jobs_raw, list):
        print(f"[WARN] Could not fetch protection jobs from {cohesity_cluster} - skipping Cohesity protection, add manually.", file=sys.stderr)
        return

    jobs = [j for j in jobs_raw if j.get("name") == job_name]
    if not jobs:
        print(f"[WARN] No Cohesity protection job named '{job_name}' found on {cohesity_cluster} - skipping. Create the job first, or protect manually.", file=sys.stderr)
        return
    job_id = jobs[0]["id"]

    job_detail = client.job(job_id)
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

    client.refresh_netapp_source(args.cluster)

    source_id = None
    max_attempts = 12
    for attempt in range(1, max_attempts + 1):
        source_id = client.find_volume_source_id(args.cluster, args.svm_name, args.volume_name)
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

    updated_job = dict(job_detail)
    updated_job["sourceIds"] = sorted(set(existing_source_ids + [source_id]))
    if template_param is not None:
        new_param = dict(template_param)
        new_param["sourceId"] = source_id
        updated_job["sourceSpecialParameters"] = special_params + [new_param]

    response = client.update_job(job_id, updated_job)
    if isinstance(response, dict) and response.get("id"):
        print(f"[INFO] Volume '{args.volume_name}' added to Cohesity protection job '{job_name}' on {cohesity_cluster}.")
    else:
        print(f"[WARN] Failed to add '{args.volume_name}' to Cohesity job '{job_name}' - {_error_message(response)}", file=sys.stderr)
        print("[WARN] The volume itself was created successfully. Protect it manually.", file=sys.stderr)


def unprotect_volume(args):
    """Removes a just-deleted volume's sourceId from any Cohesity
    protection job(s) that reference it, cleaning up the stale reference
    'volume delete' would otherwise leave behind. Does NOT delete any
    backup snapshot data already taken for that volume - that stays until
    Cohesity's own retention policy expires it, or someone removes it
    explicitly via Cohesity; deleting real backup data is not something
    this should ever do silently as a side effect of an ONTAP delete.
    Never fatal - the ONTAP volume is already gone by the time this runs.

    Caveat: this only finds the volume if Cohesity's own discovery tree
    still lists it as a source. If Cohesity's periodic scan has already
    run and dropped it since the ONTAP delete (a race, and an unlikely one
    given scan intervals are normally minutes), a stale sourceId can be
    left in the job - same "discovery can lag" caveat documented for
    'volume check'."""
    print("")

    if getattr(args, "dry_run", False):
        print(f"[DRY-RUN] Would check Cohesity for protection job(s) referencing deleted volume '{args.volume_name}' and remove it from any that are found (no API calls made, no API key needed).")
        return

    print(f"[INFO] Checking Cohesity protection for deleted volume '{args.volume_name}'...")

    if not HAVE_REQUESTS:
        print("[WARN] Python 'requests' package not found - skipping Cohesity cleanup.", file=sys.stderr)
        return

    cohesity_cluster = resolve_cohesity_cluster(args)
    if cohesity_cluster is None:
        print(f"[WARN] No known Cohesity mapping for cluster '{args.cluster}' - pass --cohesity-cluster to clean up manually. Skipping.", file=sys.stderr)
        return

    client = connect(args, cohesity_cluster)

    source_id = client.find_volume_source_id(args.cluster, args.svm_name, args.volume_name)
    if source_id is None:
        print(f"[INFO] '{args.volume_name}' was not found in Cohesity's source tree - nothing to unregister (or its discovery scan already dropped it).")
        return

    jobs_raw = client.active_jobs()
    if not isinstance(jobs_raw, list):
        print(f"[WARN] Could not fetch protection jobs from {cohesity_cluster} to check protection status.", file=sys.stderr)
        return

    removed_from = []
    for jid, detail in iter_netapp_job_details(client, jobs_raw):
        job_name = detail.get("name")
        source_ids = detail.get("sourceIds") or []
        if source_id not in source_ids:
            continue

        if not args.assume_yes:
            confirm = input(f"Remove deleted volume '{args.volume_name}' from Cohesity job '{job_name}' on {cohesity_cluster}? [y/N] ")
            if confirm not in ("y", "Y"):
                print(f"[INFO] Left '{args.volume_name}' registered in '{job_name}' (not confirmed).")
                continue

        updated_job = dict(detail)
        updated_job["sourceIds"] = [sid for sid in source_ids if sid != source_id]
        special_params = detail.get("sourceSpecialParameters")
        if special_params:
            updated_job["sourceSpecialParameters"] = [p for p in special_params if p.get("sourceId") != source_id]

        response = client.update_job(jid, updated_job)
        if isinstance(response, dict) and response.get("id"):
            print(f"[INFO] Removed '{args.volume_name}' from Cohesity protection job '{job_name}'.")
            removed_from.append(job_name)
        else:
            print(f"[WARN] Failed to remove '{args.volume_name}' from Cohesity job '{job_name}' - {_error_message(response)}", file=sys.stderr)

    if not removed_from:
        print(f"[INFO] '{args.volume_name}' was not registered in any active Cohesity job on {cohesity_cluster}.")


def print_protection_status(args):
    """Cohesity half of 'volume check': discovery + which job(s) protect it."""
    print("")
    print("[Cohesity]")

    if not HAVE_REQUESTS:
        print("  Python 'requests' package not found - cannot check Cohesity status.")
        return

    cohesity_cluster = resolve_cohesity_cluster(args)
    if cohesity_cluster is None:
        print(f"  No known Cohesity mapping for cluster '{args.cluster}' - pass --cohesity-cluster to check. Skipping.")
        return

    client = connect(args, cohesity_cluster)

    source_id = client.find_volume_source_id(args.cluster, args.svm_name, args.volume_name)
    if source_id is None:
        print(f"  Not discovered in Cohesity's source tree on {cohesity_cluster}.")
        print("  (Discovery can lag a real creation by a few minutes, or the source may need a refresh - see 'volume backup'.)")
        return
    print(f"  Discovered on {cohesity_cluster} (source id: {source_id}).")

    jobs_raw = client.active_jobs()
    if not isinstance(jobs_raw, list):
        print(f"  Could not fetch protection jobs from {cohesity_cluster} to check protection status.")
        return

    matches = [detail.get("name") for _, detail in iter_netapp_job_details(client, jobs_raw)
               if source_id in (detail.get("sourceIds") or [])]

    if matches:
        print("  Protected by:")
        for name in matches:
            print(f"    - {name}")
    else:
        print(f"  NOT currently protected by any active Cohesity job on {cohesity_cluster}.")
