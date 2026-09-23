"""Fixed values that are part of the tool's behavior rather than of the
site. Site-specific values (snapshot policies, Cohesity mapping, domain,
...) live in the config file instead - see config.py."""

VALID_SIZE_UNITS = ["B", "KB", "MB", "GB", "TB", "PB"]

BACKUP_TIERS = ("short", "mid", "long", "none")
