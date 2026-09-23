"""Site configuration (config.yaml) and secrets (.env).

Same conventions as cos2pag: plain YAML, any ``${VAR_NAME}`` string is
replaced with that environment variable at load time, and a .env file is
loaded into the environment first so secrets never live in the YAML.

The configuration is process-wide: cli.main() calls load() once, and
settings() returns it (loading the bundled default_config.yaml on first
use if nothing was loaded explicitly, e.g. from tests).
"""

import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml
from dotenv import load_dotenv

DEFAULT_CONFIG = Path(__file__).with_name("default_config.yaml")
CONFIG_SEARCH_PATH = [
    Path("~/.config/netapp/config.yaml").expanduser(),
    Path("/etc/netapp/config.yaml"),
]
USER_ENV_FILE = Path("~/.config/netapp/.env").expanduser()

_ENV_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Settings:
    source: str
    domain: str
    snap_policies: tuple
    snap_policy_protocols: dict
    cohesity_backup_network_prefix: str
    cohesity_clusters: dict  # NetApp cluster -> (cohesity_cluster, job_suffix)


def _interpolate(value):
    if isinstance(value, str):
        def repl(match):
            name = match.group(1)
            if name not in os.environ:
                raise ConfigError(f"Environment variable '{name}' referenced in config is not set")
            return os.environ[name]

        return _ENV_VAR_RE.sub(repl, value)
    if isinstance(value, dict):
        return {k: _interpolate(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate(v) for v in value]
    return value


def _require(d, key, section):
    if not isinstance(d, dict) or d.get(key) in (None, ""):
        raise ConfigError(f"Missing required config key '{section}.{key}'")
    return d[key]


def parse(raw, source):
    if not isinstance(raw, dict):
        raise ConfigError(f"Config file '{source}' does not contain a YAML mapping")
    raw = _interpolate(raw)

    ontap = raw.get("ontap") or {}
    snaps = raw.get("snapshot_policies") or {}
    cohesity = raw.get("cohesity") or {}

    valid = _require(snaps, "valid", "snapshot_policies")
    if not isinstance(valid, list):
        raise ConfigError("'snapshot_policies.valid' must be a list")
    restrictions = snaps.get("protocol_restrictions") or {}
    for policy, protocol in restrictions.items():
        if protocol not in ("nfs", "cifs"):
            raise ConfigError(f"'snapshot_policies.protocol_restrictions.{policy}' must be nfs or cifs (got '{protocol}')")

    clusters = {}
    for cluster, entry in (cohesity.get("clusters") or {}).items():
        section = f"cohesity.clusters.{cluster}"
        clusters[cluster] = (_require(entry, "cohesity_cluster", section), _require(entry, "job_suffix", section))

    return Settings(
        source=str(source),
        domain=_require(ontap, "domain", "ontap"),
        snap_policies=tuple(str(p) for p in valid),
        snap_policy_protocols=dict(restrictions),
        cohesity_backup_network_prefix=str(_require(cohesity, "backup_network_prefix", "cohesity")),
        cohesity_clusters=clusters,
    )


def find_config(explicit=None):
    """--config, else $NETAPP_CONFIG, else the first of CONFIG_SEARCH_PATH
    that exists, else the bundled default."""
    if explicit:
        return Path(explicit)
    if os.environ.get("NETAPP_CONFIG"):
        return Path(os.environ["NETAPP_CONFIG"])
    for path in CONFIG_SEARCH_PATH:
        if path.is_file():
            return path
    return DEFAULT_CONFIG


def load_env_file(explicit=None):
    """Loads secrets into os.environ: --env-file if given, else ./.env if
    present, else ~/.config/netapp/.env if present. override=True, same as
    cos2pag: the file is authoritative over a stale value exported earlier
    in the shell."""
    if explicit:
        if not Path(explicit).is_file():
            raise ConfigError(f"--env-file '{explicit}' does not exist")
        load_dotenv(explicit, override=True)
        return explicit
    for path in (Path(".env"), USER_ENV_FILE):
        if path.is_file():
            load_dotenv(path, override=True)
            return str(path)
    return None


_current = None


def load(path=None):
    global _current
    path = find_config(path)
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    except OSError as exc:
        raise ConfigError(f"Cannot read config file '{path}': {exc.strerror}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"Config file '{path}' is not valid YAML: {exc}") from exc
    _current = parse(raw, path)
    return _current


def settings():
    return _current if _current is not None else load(DEFAULT_CONFIG)
