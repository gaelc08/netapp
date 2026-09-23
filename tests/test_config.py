import pytest

from netapp import config
from netapp.cli import main

MINIMAL = """
ontap:
  domain: example.org
snapshot_policies:
  valid: [gold, silver]
  protocol_restrictions: {gold: cifs}
cohesity:
  backup_network_prefix: "10.9."
  clusters:
    nas-a: {cohesity_cluster: coh-a, job_suffix: XA}
"""


def test_bundled_default_config_matches_the_historical_values():
    s = config.load()
    assert s.source == str(config.DEFAULT_CONFIG)
    assert s.domain == "ctie.etat.lu"
    assert "CTIE_Prod" in s.snap_policies and "none" in s.snap_policies
    assert s.snap_policy_protocols == {"CTIE_Prod": "cifs"}
    assert s.cohesity_backup_network_prefix == "10.111.210."
    assert s.cohesity_clusters["damascus-4"] == ("closluce-2", "CS3")


def test_config_lookup_order(tmp_path, monkeypatch):
    user = tmp_path / "user.yaml"
    env = tmp_path / "env.yaml"
    flag = tmp_path / "flag.yaml"
    assert config.find_config() == config.DEFAULT_CONFIG
    monkeypatch.setattr(config, "CONFIG_SEARCH_PATH", [user])
    user.write_text(MINIMAL)
    assert config.find_config() == user
    monkeypatch.setenv("NETAPP_CONFIG", str(env))
    assert config.find_config() == env
    assert config.find_config(str(flag)) == flag


def test_custom_config_drives_validation_and_cohesity_mapping(tmp_path, fakes, capsys):
    cfg = tmp_path / "site.yaml"
    cfg.write_text(MINIMAL)
    with pytest.raises(SystemExit):
        main(["volume", "backup", "--config", str(cfg), "--cluster", "nas-a", "--svm", "s", "--volume", "v",
              "--backup-tier", "long", "--dry-run"])
    assert "Cohesity job 'Nas-a-LongTerm-XA' on cluster 'coh-a'" in capsys.readouterr().out

    with pytest.raises(SystemExit):
        main(["volume", "create", "--config", str(cfg), "--type", "nfs", "--user", "u", "--cluster", "nas-a",
              "--svm", "s", "--volume", "v", "--size", "1", "--aggregate", "a", "--snap-policy", "gold",
              "--client-match", "1.2.3.4"])
    assert "--snap-policy gold is only valid for --type cifs" in capsys.readouterr().err


def test_env_placeholders_are_interpolated(tmp_path, monkeypatch):
    cfg = tmp_path / "site.yaml"
    cfg.write_text(MINIMAL.replace("example.org", "${SITE_DOMAIN}"))
    monkeypatch.setenv("SITE_DOMAIN", "corp.local")
    assert config.load(str(cfg)).domain == "corp.local"
    monkeypatch.delenv("SITE_DOMAIN")
    with pytest.raises(config.ConfigError, match="'SITE_DOMAIN' referenced in config is not set"):
        config.load(str(cfg))


@pytest.mark.parametrize("broken, message", [
    ("domain: example.org", "Missing required config key 'ontap.domain'"),
    ("{gold: cifs}", "must be nfs or cifs"),
    ("job_suffix: XA", "Missing required config key 'cohesity.clusters.nas-a.job_suffix'"),
])
def test_invalid_config_is_reported(tmp_path, fakes, capsys, broken, message):
    replacement = {"domain: example.org": "other: x", "{gold: cifs}": "{gold: smb}", "job_suffix: XA": "x: y"}[broken]
    cfg = tmp_path / "bad.yaml"
    cfg.write_text(MINIMAL.replace(broken, replacement))
    with pytest.raises(SystemExit) as exc:
        main(["aggregate", "list", "--config", str(cfg), "--user", "u", "--cluster", "c"])
    assert exc.value.code == 1
    assert message in capsys.readouterr().err


def test_missing_config_file_is_reported(fakes, capsys):
    with pytest.raises(SystemExit):
        main(["aggregate", "list", "--config", "/nope.yaml", "--user", "u", "--cluster", "c"])
    assert "Cannot read config file '/nope.yaml'" in capsys.readouterr().err


def test_dotenv_supplies_secrets(tmp_path, fakes, monkeypatch):
    (tmp_path / ".env").write_text("COHESITY_APIKEY=from-dotenv\n")
    monkeypatch.delenv("COHESITY_APIKEY", raising=False)
    fakes.on_http("GET", "/protectionSources", 200, [])
    with pytest.raises(SystemExit):
        main(["volume", "check", "--user", "u", "--cluster", "damascus-3", "--svm", "s", "--volume", "v"])
    cohesity_calls = [c for c in fakes.http_calls() if "irisservices" in c[2]]
    assert cohesity_calls and all(c[3]["headers"]["apiKey"] == "from-dotenv" for c in cohesity_calls)
    monkeypatch.delenv("COHESITY_APIKEY", raising=False)


def test_explicit_env_file_must_exist(fakes, capsys):
    with pytest.raises(SystemExit):
        main(["aggregate", "list", "--env-file", "/nope.env", "--user", "u", "--cluster", "c"])
    assert "--env-file '/nope.env' does not exist" in capsys.readouterr().err
