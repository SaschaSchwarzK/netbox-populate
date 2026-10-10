"""Offline tests for the engine (no NetBox needed): pytest tests/"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import populate  # noqa: E402


class FakeRecord:
    def __init__(self, **fields):
        self.__dict__.update(fields)


class FakeEndpoint:
    def __init__(self, records):
        self.records = records

    def all(self):
        return list(self.records)


@pytest.fixture
def engine():
    return populate.Populator(nb=None, dry_run=False, verbose=False, fail_fast=False)


def test_data_files_are_valid():
    files = sorted((Path(__file__).resolve().parent.parent / "data").glob("*.yml"))
    assert files, "no data files found"
    assert populate.validate_files(files) == 0


def test_parse_query_decodes_values(engine):
    assert engine.parse_query("name=Mordor%20Underlay&site=a%26b") == {"name": "Mordor Underlay", "site": "a&b"}


def test_parse_query_requires_a_filter(engine):
    with pytest.raises(populate.DataError):
        engine.parse_query("")


def test_literal_at_sign_is_not_a_reference(engine):
    assert engine.resolve("@@mention") == "@mention"
    assert engine.resolve({"a": ["plain", 3, True]}) == {"a": ["plain", 3, True]}


def test_malformed_reference_is_rejected(engine):
    with pytest.raises(populate.DataError):
        engine.resolve("@not-a-valid-reference")


def test_lookup_filter_name_for_references(engine):
    spec = {"lookup": ["device", "name", "interface_id"]}
    raw = {"device": "@dcim/devices?name=x", "name": "eth0", "interface_id": "@dcim/interfaces?name=y"}
    payload = {"device": 5, "name": "eth0", "interface_id": 9}
    assert engine.lookup_params(spec, raw, payload) == {"device_id": 5, "name": "eth0", "interface_id": 9}


def test_optional_lookup_fields_are_skipped(engine):
    spec = {"lookup": ["address", "vrf"]}
    assert engine.lookup_params(spec, {"address": "10.0.0.1/24"}, {"address": "10.0.0.1/24"}) == {"address": "10.0.0.1/24"}


def test_lookup_requires_at_least_one_field(engine):
    with pytest.raises(populate.DataError):
        engine.lookup_params({"lookup": ["name"]}, {"slug": "x"}, {"slug": "x"})


def test_tags_become_slug_references_unless_plain():
    payload = {"tags": ["pied-piper", {"name": "x"}]}
    populate.Populator.normalize_tags(payload, plain=False)
    assert payload["tags"] == [{"slug": "pied-piper"}, {"name": "x"}]
    plain = {"tags": ["pied-piper"]}
    populate.Populator.normalize_tags(plain, plain=True)
    assert plain["tags"] == ["pied-piper"]


def test_verify_match_detects_ignored_filters():
    spec = {"lookup": ["name"]}
    record = FakeRecord(name="Rebel Command")
    with pytest.raises(populate.DataError):
        populate.Populator.verify_match(record, spec, {"name": "Watchers"}, {"name": "Watchers"})
    populate.Populator.verify_match(record, spec, {"name": "rebel command"}, {"name": "rebel command"})


def test_scan_lookup_compares_locally(engine):
    ep = FakeEndpoint([FakeRecord(user=FakeRecord(id=1), object_id=5), FakeRecord(user=FakeRecord(id=1), object_id=7)])
    spec = {"lookup": ["user", "object_id"], "lookup_mode": "scan"}
    assert engine.scan_existing(ep, spec, {"user": 1, "object_id": 7}).object_id == 7
    assert engine.scan_existing(ep, spec, {"user": 1, "object_id": 9}) is None


def test_validate_reports_problems(tmp_path):
    bad = tmp_path / "010-bad.yml"
    bad.write_text("endpoint: nope\nmode: weird\nobjects:\n  - {name: a, site: '@broken'}\n")
    assert populate.validate_files([bad]) == 1
