# netapp

A NetApp ONTAP volume management CLI, with optional Cohesity backup
registration. Python port of `netapp_volume_create.sh`, restructured into
`netapp <resource> <action>` subcommands once it grew past "just create
volumes", then split from a single script into a package.

## Requirements

- Python 3.8+
- `requests` (used for the ONTAP REST API mode and all Cohesity calls)
- For the default `--api ssh` mode: passwordless SSH key access to the
  target cluster (`<cluster>.ctie.etat.lu`), the same as the original bash
  script required.

## Installing / running

```bash
pip install .            # installs a `netapp` command on PATH
netapp -h

# or straight from a checkout, without installing:
pip install -r requirements.txt
./bin/netapp -h          # or: python3 -m netapp -h
```

## Layout

```
netapp/
  cli.py          argument parsing and subcommand dispatch
  constants.py    site-specific values (snapshot policies, Cohesity mapping, ...)
  validation.py   --size parsing/padding and 'volume create' input checks
  rollback.py     single undo stack shared by the ssh and REST paths
  ontap_ssh.py    ONTAP over ssh (default --api)
  ontap_rest.py   ONTAP over its REST API (--api rest)
  volume.py       volume create/delete/check orchestration
  aggregate.py    aggregate listing / interactive selection
  access.py       client access info block (LIFs, mount point, firewall)
  cohesity.py     Cohesity client + protect/unprotect/status
  util.py         small shared helpers
tests/            pytest suite (ssh, HTTP and prompts are all faked)
bin/netapp        launcher for running from a checkout
```

Run the tests with `pip install -e '.[dev]' && pytest`.

## Commands

```
netapp aggregate list     Show aggregate occupancy
netapp volume create      Create an NFS or CIFS volume (registers it with
                           Cohesity afterward, unless --no-backup)
netapp volume delete      Delete a volume (and its export-policy, if safe)
netapp volume check       Read-only ONTAP + Cohesity status report
netapp volume backup      Register an already-existing volume with Cohesity
                           (the recovery path after a failed/skipped backup
                           step on 'volume create')
```

Run `netapp <command> -h` for that command's own options, or `netapp -h`
for the full picture (auth chains, Cohesity cluster/job mapping, etc).

## Examples

```bash
netapp aggregate list --user admin --cluster damascus-3

netapp volume create --type nfs --user admin --cluster damascus-3 \
    --svm svm1 --volume vol1 --size 100 --aggregate aggr1 \
    --snap-policy CTIE_default --client-match 10.0.0.0/24 \
    --comment 'Test volume'

netapp volume delete --user admin --cluster damascus-3 --svm svm1 --volume vol1

netapp volume check --user admin --cluster damascus-3 --svm svm1 --volume vol1
```

Add `--dry-run` to any of `volume create` / `volume delete` / `volume backup`
to print what would happen without making any ONTAP or Cohesity call.

## ONTAP access: `--api ssh` (default) vs `--api rest`

- **`ssh`** (default) — runs the same CLI commands an admin would type by
  hand, over an ssh connection authenticated by your own key/agent. This is
  the fully-covered path: all five commands work.
- **`rest`** — talks to ONTAP's REST API over HTTPS instead. Implemented
  for `aggregate list`, `volume create` (NFS and CIFS, including the
  tiering-policy PATCH, the mount step, and home-directory quota for CIFS),
  and `volume delete`, using async job polling where ONTAP requires it.
  `volume check` is not yet ported and rejects `--api rest`.

  Authentication for `--api rest`, tried in this order:
  1. **Client certificate**, if configured: `--cert-file`/`--key-file` (or
     `$ONTAP_CERT_FILE`/`$ONTAP_KEY_FILE`). The cert must already be
     installed in ONTAP and mapped to a user (`security login create
     -authmethod cert -application http`).
  2. **HTTP basic auth** otherwise: `--user` plus `--password` (or
     `$ONTAP_PASSWORD`, or a hidden prompt if neither is given).

  TLS is verified by default. Most intranet clusters present a
  self-signed/internal-CA cert, so you'll likely need either
  `--ca-bundle <path-to-your-CA>` or, for quick testing only,
  `--insecure-ontap`.

## Cohesity backup

On by default after `volume create` (`--no-backup` to skip), and available
standalone via `volume backup` for retrying a registration that failed the
first time. Auth is `--cohesity-apikey`, or `$COHESITY_APIKEY`, or a hidden
prompt.

The Cohesity cluster and job name are derived from `--cluster` and
`--backup-tier`:

| `--cluster`            | Cohesity cluster | Job name pattern              |
|-------------------------|------------------|--------------------------------|
| `damascus-3`, `jericho-1` | `closluce-1`    | `<Cluster>-<Tier>Term-DC1`     |
| `damascus-4`, `jericho-2` | `closluce-2`    | `<Cluster>-<Tier>Term-CS3`     |

For any other cluster, pass both `--cohesity-cluster` and `--cohesity-job`
explicitly, or backup registration is skipped with a warning.

`volume delete` automatically removes the deleted volume from any Cohesity
job it was registered in afterward (`--no-unprotect` to skip). This only
unregisters the stale reference - it does **not** delete any backup
snapshot data already taken; that stays until Cohesity's own retention
policy expires it, or someone removes it explicitly via Cohesity.

## Snapshot policies

Valid values for `--snap-policy`: `CTIE_daily`, `CTIE_daily_315`,
`CTIE_default`, `CTIE_heavy`, `CTIE_light`, `CTIE_medium`,
`CTIE_one_weekly`, `CTIE_Prod`, `none`.

`CTIE_Prod` (hourly x24, daily x31, weekly x4) is CIFS-only by convention -
`volume create --type nfs --snap-policy CTIE_Prod` is rejected before any
ONTAP call is made.

## Known gaps

- `volume check` doesn't have a REST implementation yet - it always uses
  ssh regardless of `--api`.
- The REST path (`--api rest`) has been tested against a real ONTAP
  cluster for `aggregate list`, `volume create`, and `volume delete`, but
  not exhaustively - review `--dry-run` output before trusting it for a
  case this hasn't seen yet.
