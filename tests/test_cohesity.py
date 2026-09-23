import pytest

from netapp.cli import main
from netapp.cohesity import CohesityClient

from .conftest import cohesity_tree

BASE = "https://closluce-1/irisservices/api/v1/public"
BACKUP = ["volume", "backup", "--cluster", "damascus-3", "--svm", "svm1", "--volume", "vol1",
          "--backup-tier", "short", "--cohesity-apikey", "k", "--yes"]


def _cohesity(fakes, job_source_ids=(1,)):
    fakes.on_http("POST", "/protectionSources/refresh/7", 204)
    fakes.on_http("GET", "/protectionSources?environments=kNetapp", 200, cohesity_tree())
    fakes.on_http("GET", "/protectionJobs?isDeleted=false&isActive=true", 200, [
        {"id": 1, "name": "Damascus-3-ShortTerm-DC1", "environment": "kNetapp"},
        {"id": 3, "name": "VMs", "environment": "kVMware"},
    ])
    fakes.on_http("GET", "/protectionJobs/1", 200, {
        "id": 1, "name": "Damascus-3-ShortTerm-DC1", "sourceIds": list(job_source_ids),
        "sourceSpecialParameters": [{"sourceId": 1, "opt": "x"}],
    })
    fakes.on_http("PUT", "/protectionJobs/1", 200, {"id": 1})


def test_find_volume_source_id_matches_cluster_svm_and_volume(fakes):
    fakes.on_http("GET", "/protectionSources", 200, cohesity_tree())
    client = CohesityClient("closluce-1", "k")
    assert client.find_volume_source_id("damascus-3", "svm1", "vol1") == 42
    assert client.find_volume_source_id("damascus-3", "svm2", "vol1") is None


def test_backup_adds_volume_to_job_derived_from_cluster_and_tier(fakes, capsys):
    _cohesity(fakes)
    with pytest.raises(SystemExit):
        main(BACKUP)
    put = [c for c in fakes.http_calls() if c[1] == "PUT"][0]
    assert put[2] == f"{BASE}/protectionJobs/1"
    assert '"sourceIds": [1, 42]' in put[3]["data"]
    assert '{"sourceId": 42, "opt": "x"}' in put[3]["data"]
    assert "added to Cohesity protection job 'Damascus-3-ShortTerm-DC1'" in capsys.readouterr().out


def test_backup_is_noop_when_already_protected(fakes, capsys):
    _cohesity(fakes, job_source_ids=(1, 42))
    with pytest.raises(SystemExit):
        main(BACKUP)
    assert not [c for c in fakes.http_calls() if c[1] == "PUT"]
    assert "already protected" in capsys.readouterr().out


def test_backup_dry_run_makes_no_call(fakes, capsys):
    with pytest.raises(SystemExit):
        main(BACKUP + ["--dry-run"])
    assert fakes.http_calls() == []
    assert "Would register 'vol1' with Cohesity job 'Damascus-3-ShortTerm-DC1' on cluster 'closluce-1'" in capsys.readouterr().out


def test_backup_unmapped_cluster_is_skipped(fakes, capsys):
    with pytest.raises(SystemExit):
        main(["volume", "backup", "--cluster", "elsewhere", "--svm", "s", "--volume", "v", "--backup-tier", "short"])
    assert fakes.http_calls() == []
    assert "No known Cohesity mapping" in capsys.readouterr().err


# ── TLS verification ────────────────────────────────────────────────────────

def _cohesity_verify_values(fakes):
    return {c[3]["verify"] for c in fakes.http_calls() if "irisservices" in c[2]}


def test_tls_verified_by_default(fakes):
    _cohesity(fakes)
    with pytest.raises(SystemExit):
        main(BACKUP)
    assert _cohesity_verify_values(fakes) == {True}


def test_ca_bundle_flag(fakes, tmp_path):
    ca = tmp_path / "ca.pem"
    ca.write_text("x")
    _cohesity(fakes)
    with pytest.raises(SystemExit):
        main(BACKUP + ["--cohesity-ca-bundle", str(ca)])
    assert _cohesity_verify_values(fakes) == {str(ca)}


def test_ca_bundle_env(fakes, tmp_path, monkeypatch):
    ca = tmp_path / "ca.pem"
    ca.write_text("x")
    monkeypatch.setenv("COHESITY_CA_BUNDLE", str(ca))
    _cohesity(fakes)
    with pytest.raises(SystemExit):
        main(BACKUP)
    assert _cohesity_verify_values(fakes) == {str(ca)}


def test_missing_ca_bundle_is_an_error(fakes, capsys):
    with pytest.raises(SystemExit) as exc:
        main(BACKUP + ["--cohesity-ca-bundle", "/nope/ca.pem"])
    assert exc.value.code == 1
    assert "Cohesity CA bundle '/nope/ca.pem' does not exist" in capsys.readouterr().err
    assert fakes.http_calls() == []


def test_insecure_opt_out(fakes):
    _cohesity(fakes)
    with pytest.raises(SystemExit):
        main(BACKUP + ["--insecure-cohesity"])
    assert _cohesity_verify_values(fakes) == {False}


def test_tls_failure_explains_the_fix_once(fakes, capsys):
    import requests
    fakes.on_http("GET", "/protectionJobs", raises=requests.exceptions.SSLError("certificate verify failed"))
    with pytest.raises(SystemExit):
        main(BACKUP)
    err = capsys.readouterr().err
    assert err.count("TLS verification of closluce-1 failed") == 1
    assert "--cohesity-ca-bundle" in err
    assert "Could not fetch protection jobs" in err
