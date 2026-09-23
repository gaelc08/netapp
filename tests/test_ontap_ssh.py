from netapp.cli import main
from netapp.ontap_ssh import parse_ontap_fields

from .conftest import ontap_fields_output

NFS_CREATE = ["volume", "create", "--type", "nfs", "--user", "admin", "--cluster", "damascus-3",
              "--svm", "svm1", "--volume", "vol1", "--size", "90", "--aggregate", "aggr1",
              "--snap-policy", "CTIE_default", "--client-match", "10.0.0.0/24", "--no-backup"]


def test_parse_ontap_fields_uses_header_order_not_request_order():
    raw = "vserver:junction-path:policy\r\nVserver:Junction Path:Policy\r\nsvm1:/vol1:pol1\r\n"
    assert parse_ontap_fields(raw, ["policy", "junction-path", "missing"]) == [("pol1", "/vol1", "")]


def test_parse_ontap_fields_no_rows():
    assert parse_ontap_fields("There are no entries matching your query.\n", ["policy"]) == []


def test_nfs_create_runs_steps_in_order(fakes):
    fakes.on_ssh("net interface show", ontap_fields_output(["address"], ["svm1", "10.0.0.5"]))
    main(NFS_CREATE)
    cmds = fakes.ssh_commands()
    assert cmds[0] == "export-policy create -vserver svm1 -policyname vol1"
    assert cmds[1].startswith("export-policy rule create -vserver svm1 -policyname vol1 -clientmatch 10.0.0.0/24")
    assert cmds[2].startswith("volume create -vserver svm1 -volume vol1 -aggregate aggr1 -size 100.00GB")


def test_nfs_create_rolls_back_export_policy_when_volume_create_fails(fakes, capsys):
    fakes.on_ssh("volume create", stderr="Error: no space", returncode=1)
    try:
        main(NFS_CREATE)
    except SystemExit as exc:
        assert exc.code == 1
    assert fakes.ssh_commands()[-1] == "set -confirmations off; export-policy delete -vserver svm1 -policyname vol1"
    err = capsys.readouterr().err
    assert "Rolling back 1 already-completed step(s)" in err
    assert "Failed to create volume - rolled back" in err


def test_nfs_create_dry_run_sends_nothing(fakes, capsys):
    main(NFS_CREATE + ["--dry-run"])
    # Only the read-only LIF lookup for the access-info block runs for real.
    assert all("net interface show" in c for c in fakes.ssh_commands())
    assert "[DRY-RUN] Would run on damascus-3: export-policy create" in capsys.readouterr().out


def test_delete_keeps_export_policy_still_used_elsewhere(fakes, capsys):
    fakes.on_ssh("volume show -vserver svm1 -volume vol1",
                 ontap_fields_output(["policy", "junction-path"], ["svm1", "shared", "/vol1"]))
    fakes.on_ssh("volume show -vserver svm1 -fields volume,policy",
                 ontap_fields_output(["volume", "policy"], ["svm1", "vol2", "shared"]))
    try:
        main(["volume", "delete", "--user", "a", "--cluster", "damascus-3", "--svm", "svm1",
              "--volume", "vol1", "--yes", "--no-unprotect"])
    except SystemExit as exc:
        assert exc.code == 0
    assert not any(c.startswith("export-policy delete") for c in fakes.ssh_commands())
    assert "still used by other volume(s)" in capsys.readouterr().out


def test_delete_aborts_without_typed_confirmation(fakes):
    fakes.on_ssh("volume show", ontap_fields_output(["policy", "junction-path"], ["svm1", "p", "/vol1"]))
    fakes.inputs = ["yes"]
    try:
        main(["volume", "delete", "--user", "a", "--cluster", "damascus-3", "--svm", "svm1", "--volume", "vol1"])
    except SystemExit as exc:
        assert exc.code == 0
    assert len(fakes.ssh_commands()) == 1  # just the lookup
