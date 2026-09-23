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

## Configuration and secrets

Site-specific values - the valid `--snap-policy` names and their protocol
restrictions, the NetApp -> Cohesity cluster/job mapping, the Cohesity
backup LIF network, and the DNS domain clusters are reached under - live
in a YAML file, not in the code. The copy bundled with the package
(`netapp/default_config.yaml`) holds the current values, so nothing needs
configuring to get started. To change them, copy that file and edit it;
the first of these that exists is used, and replaces the bundled one
entirely:

1. `--config <path>`
2. `$NETAPP_CONFIG`
3. `~/.config/netapp/config.yaml`
4. `/etc/netapp/config.yaml`

As in cos2pag, any `${VAR_NAME}` in the YAML is read from the environment.

Secrets go in a `.env` file instead (see `.env.example`): `./.env` if
present, else `~/.config/netapp/.env`, or `--env-file <path>`. It can hold
`COHESITY_APIKEY`, `COHESITY_CA_BUNDLE`, `ONTAP_PASSWORD`,
`ONTAP_CERT_FILE`/`ONTAP_KEY_FILE` and `ONTAP_CA_BUNDLE`. A command-line
flag always wins over the environment.

## Layout

```
netapp/
  cli.py          argument parsing and subcommand dispatch
  config.py       config.yaml + .env loading
  default_config.yaml  bundled site config (see above)
  constants.py    fixed values (size units, backup tiers)
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

Run the checks CI runs with `pip install -e '.[dev]' && ruff check . && pytest`.

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
  `--ca-bundle <path-to-your-CA>` (or `ONTAP_CA_BUNDLE` in your `.env`)
  or, for quick testing only, `--insecure-ontap`.

## Cohesity backup

On by default after `volume create` (`--no-backup` to skip), and available
standalone via `volume backup` for retrying a registration that failed the
first time. Auth is `--cohesity-apikey`, or `$COHESITY_APIKEY`, or a hidden
prompt.

TLS to the Cohesity API is verified by default, same as ONTAP REST. If the
Cohesity clusters present an internal-CA certificate, point at that CA with
`--cohesity-ca-bundle <path>` or, more conveniently, `export
COHESITY_CA_BUNDLE=<path>` (shell profile or `.env`). `--insecure-cohesity`
skips verification entirely (not recommended; the API key is sent with
every request). A verification failure is reported explicitly rather than
as a generic "could not fetch" warning.

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
