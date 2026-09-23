"""Site-specific values. Kept in one place so they're easy to find (and,
later, to move into a config file) instead of scattered through the code."""

# LIF addresses on this network are reserved for Cohesity backup traffic
# and must never be handed out to clients as a mount target - excluded
# from the client access info block.
COHESITY_BACKUP_NETWORK_PREFIX = "10.111.210."

# CTIE_Prod schedule (from `snapshot-policy show -policy CTIE_Prod -instance`
# on damascus-3): hourly x24, daily x31, weekly x4.
VALID_SNAP_POLICIES = [
    "CTIE_daily", "CTIE_daily_315", "CTIE_default", "CTIE_heavy",
    "CTIE_light", "CTIE_medium", "CTIE_one_weekly", "CTIE_Prod", "none",
]

# Some snapshot policies are restricted to one protocol by convention - ONTAP
# itself doesn't enforce this, but picking the wrong one here is a config
# mistake worth catching before a volume ever gets created with it.
SNAP_POLICY_PROTOCOL_RESTRICTIONS = {
    "CTIE_Prod": "cifs",
}
VALID_SIZE_UNITS = ["B", "KB", "MB", "GB", "TB", "PB"]

# cluster -> (cohesity_cluster, job-name suffix)
COHESITY_CLUSTER_MAP = {
    "damascus-3": ("closluce-1", "DC1"),
    "jericho-1": ("closluce-1", "DC1"),
    "damascus-4": ("closluce-2", "CS3"),
    "jericho-2": ("closluce-2", "CS3"),
}

BACKUP_TIERS = ("short", "mid", "long", "none")

DOMAIN = "ctie.etat.lu"
