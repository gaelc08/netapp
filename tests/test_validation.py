from types import SimpleNamespace

import pytest

from netapp.validation import nfs_size_and_snapshot_reserve, padded_size, parse_size, validate_inputs


@pytest.mark.parametrize("raw, expected", [
    ("100", ("100", "GB")),
    ("1.5tb", ("1.5", "TB")),
    ("512MB", ("512", "MB")),
])
def test_parse_size(raw, expected):
    assert parse_size(raw) == expected


@pytest.mark.parametrize("raw", ["abc", "12XB", "-5", "1,5TB"])
def test_parse_size_rejects(raw, capsys):
    with pytest.raises(SystemExit) as exc:
        parse_size(raw)
    assert exc.value.code == 1
    assert "[ERROR] --size" in capsys.readouterr().err


def test_padded_size_leaves_room_for_10pct_snapshot_reserve():
    assert padded_size("90", "GB") == "100.00GB"


def test_nfs_without_snapshot_policy_is_not_padded():
    assert nfs_size_and_snapshot_reserve("none", "90", "GB") == ("90GB", 0)
    assert nfs_size_and_snapshot_reserve("CTIE_default", "90", "GB") == ("100.00GB", 10)


def _args(**overrides):
    base = dict(snap_policy="CTIE_default", volume_type="nfs", volume_name="vol_1",
                client_match="10.0.0.0/24", junction_path=None)
    base.update(overrides)
    return SimpleNamespace(**base)


def test_validate_inputs_accepts_valid_nfs():
    validate_inputs(_args())


@pytest.mark.parametrize("overrides, message", [
    ({"snap_policy": "bogus"}, "snap_policy must be one of"),
    ({"snap_policy": "CTIE_Prod"}, "only valid for --type cifs"),
    ({"volume_name": "bad-name"}, "volume name can only contain"),
    ({"volume_name": "1vol"}, "volume name can only contain"),
    ({"client_match": None}, "--client-match is required"),
    ({"volume_type": "cifs"}, "--junction-path is required"),
])
def test_validate_inputs_rejects(overrides, message, capsys):
    with pytest.raises(SystemExit):
        validate_inputs(_args(**overrides))
    assert message in capsys.readouterr().err
