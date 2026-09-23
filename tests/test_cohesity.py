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
