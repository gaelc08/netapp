"""netapp

NetApp ONTAP volume management CLI ("netapp <resource> <action>"), with
optional Cohesity backup registration. Originally a straight Python port of
netapp_volume_create.sh; restructured from single-purpose flags (--delete,
--check, --backup-only, --list-aggregates) into subcommands once it grew
past "just create volumes", then split from a single script into this
package once that script passed ~1800 lines.

Module layout:
    cli.py          argument parsing and subcommand dispatch
    config.py       config.yaml + .env loading
    constants.py    fixed values (size units, backup tiers)
    util.py         error_exit, remote_host, byte formatting, optional requests import
    validation.py   --size parsing/padding and volume create input checks
    rollback.py     single undo stack shared by the ssh and REST paths
    ontap_ssh.py    ONTAP over ssh (the default --api)
    ontap_rest.py   ONTAP over its REST API (--api rest)
    access.py       client access info block (LIFs, mount point, firewall reminder)
    aggregate.py    aggregate listing / interactive selection
    volume.py       volume create/delete/check orchestration
    cohesity.py     Cohesity client and protect/unprotect/status
"""

__version__ = "2026.09.23.10"
