"""'aggregate list' and the interactive aggregate picker for 'volume create'."""

import sys

from . import ontap_rest, ontap_ssh
from .util import error_exit


def list_aggregates(args):
    if getattr(args, "api_mode", "ssh") == "rest":
        return ontap_rest.list_aggregates(args)
    return ontap_ssh.list_aggregates(args)


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
