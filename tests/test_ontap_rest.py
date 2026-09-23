import pytest

from netapp.cli import main

JOB = {"job": {"uuid": "j1", "_links": {"self": {"href": "/api/cluster/jobs/j1"}}}}
NFS_CREATE = ["volume", "create", "--type", "nfs", "--user", "admin", "--cluster", "damascus-3",
              "--api", "rest", "--password", "pw", "--svm", "svm1", "--volume", "vol1", "--size", "90",
              "--aggregate", "aggr1", "--snap-policy", "CTIE_default", "--client-match", "10.0.0.0/24",
              "--no-backup"]


def _ontap_ok(fakes, job_state="success"):
    fakes.on_http("POST", "/api/protocols/nfs/export-policies/5/rules", 201, {})
    fakes.on_http("POST", "/api/protocols/nfs/export-policies", 201, {"records": [{"id": 5}]})
    fakes.on_http("POST", "/api/storage/volumes", 202, JOB)
    fakes.on_http("GET", "/api/cluster/jobs/j1", 200, {"state": job_state, "error": {"message": "job exploded"}})
    fakes.on_http("DELETE", "/api/protocols/nfs/export-policies/5", 200, {})
    fakes.on_http("GET", "/api/network/ip/interfaces", 200, {"records": [{"ip": {"address": "10.0.0.5"}}]})


def test_nfs_create_over_rest(fakes):
    _ontap_ok(fakes)
    main(NFS_CREATE)
    posts = [(c[2], c[3].get("json")) for c in fakes.http_calls() if c[1] == "POST"]
    assert posts[2][0].endswith("/api/storage/volumes")
    assert posts[2][1]["size"] == "100.00GB"
    assert posts[2][1]["nas"] == {"export_policy": {"name": "vol1"}, "path": "/vol1"}
    # TLS verification on by default, basic auth from --password.
    assert all(c[3]["verify"] is True and c[3]["auth"] == ("admin", "pw") for c in fakes.http_calls())


def test_failed_job_rolls_back_export_policy(fakes, capsys):
    _ontap_ok(fakes, job_state="failure")
    with pytest.raises(SystemExit):
        main(NFS_CREATE)
    last = fakes.http_calls()[-1]
    assert (last[1], last[2]) == ("DELETE", "https://damascus-3.ctie.etat.lu/api/protocols/nfs/export-policies/5")
    assert "job exploded" in capsys.readouterr().err


AGGREGATES = ["aggregate", "list", "--user", "a", "--cluster", "c", "--api", "rest", "--password", "p"]


def test_ca_bundle_and_insecure_flags(fakes, tmp_path, monkeypatch):
    ca = tmp_path / "ca.pem"
    ca.write_text("x")
    fakes.on_http("GET", "/api/storage/aggregates", 200, {"records": []})
    with pytest.raises(SystemExit):
        main(AGGREGATES + ["--ca-bundle", str(ca)])
    with pytest.raises(SystemExit):
        main(AGGREGATES + ["--insecure-ontap"])
    monkeypatch.setenv("ONTAP_CA_BUNDLE", str(ca))
    with pytest.raises(SystemExit):
        main(AGGREGATES)
    assert [c[3]["verify"] for c in fakes.http_calls()] == [str(ca), False, str(ca)]


def test_missing_ca_bundle_is_a_clean_error_not_a_traceback(fakes, capsys):
    with pytest.raises(SystemExit) as exc:
        main(AGGREGATES + ["--ca-bundle", "/nope/ca.pem"])
    assert exc.value.code == 1
    assert "ONTAP CA bundle '/nope/ca.pem' does not exist" in capsys.readouterr().err
    assert fakes.http_calls() == []


def test_cert_file_and_key_file_must_come_together(fakes, capsys):
    with pytest.raises(SystemExit):
        main(NFS_CREATE + ["--cert-file", "/x"])
    assert "--cert-file and --key-file must both be given" in capsys.readouterr().err
