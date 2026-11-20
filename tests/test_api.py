"""End-to-end tests for the tariff-group supply API."""
import threading

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

BASE_AMOUNTS = [
    {"year": 2023, "code": "A", "amount_cents": 100},
    {"year": 2023, "code": "B", "amount_cents": 200},
    {"year": 2023, "code": "C", "amount_cents": 300},
    {"year": 2023, "code": "D", "amount_cents": 400},
    {"year": 2024, "code": "A", "amount_cents": 110},
    {"year": 2024, "code": "B", "amount_cents": 210},
    {"year": 2024, "code": "C", "amount_cents": 310},
    {"year": 2024, "code": "D", "amount_cents": 410},
]

# v1: C and D form one merged group {C,D | C,D}; A and B stay alone.
V1_ENTRIES = [
    {"old_code": "A", "new_code": "A"},
    {"old_code": "B", "new_code": "B"},
    {"old_code": "C", "new_code": "C"},
    {"old_code": "C", "new_code": "D"},
    {"old_code": "D", "new_code": "D"},
]
# v2: the C/D group is split back into single-code groups.
V2_ENTRIES = [{"old_code": c, "new_code": c} for c in "ABCD"]


def put_base(client, amounts=BASE_AMOUNTS):
    return client.put("/base", json={"amounts": amounts})


def post_mapping(client, version, entries):
    return client.post("/mappings", json={"version": version, "entries": entries})


@pytest.fixture()
def client(tmp_path):
    return TestClient(create_app(str(tmp_path / "test.db")))


@pytest.fixture()
def seeded(client):
    assert put_base(client).status_code == 200
    return client


@pytest.fixture()
def at_v1(seeded):
    assert post_mapping(seeded, 1, V1_ENTRIES).status_code == 201
    return seeded


# ------------------------------------------------------------------ base
def test_put_base_ok_and_replaceable_before_versions(seeded):
    r = put_base(seeded)  # no versions yet -> replace is allowed
    assert r.status_code == 200
    body = r.json()
    assert body["years"] == [2023, 2024]
    assert body["codes_per_year"] == {"2023": 4, "2024": 4}
    listed = seeded.get("/base").json()
    assert listed["years"] == [2023, 2024]
    assert len(listed["amounts"]) == 8


def test_base_fixed_once_version_exists(at_v1):
    r = put_base(at_v1)
    assert r.status_code == 409


@pytest.mark.parametrize(
    "amounts",
    [
        [{"year": 2023, "code": "A", "amount_cents": 1}],  # single year
        [  # non-adjacent years
            {"year": 2023, "code": "A", "amount_cents": 1},
            {"year": 2025, "code": "A", "amount_cents": 1},
        ],
        [  # duplicate (year, code)
            {"year": 2023, "code": "A", "amount_cents": 1},
            {"year": 2023, "code": "A", "amount_cents": 2},
            {"year": 2024, "code": "A", "amount_cents": 1},
        ],
    ],
)
def test_base_validation(client, amounts):
    assert put_base(client, amounts).status_code == 422


def test_mapping_requires_base(client):
    assert post_mapping(client, 1, V1_ENTRIES).status_code == 409


# --------------------------------------------------------------- grouping
def test_first_version_returns_full_changes(at_v1):
    record = at_v1.get("/changes/1").json()
    assert record["replaced_version"] is None
    assert record["removed_group_ids"] == []
    by_id = {g["group_id"]: g for g in record["summaries"]}
    assert set(by_id) == {1, 2, 3}
    assert by_id[1]["old_codes"] == ["A"] and by_id[1]["new_codes"] == ["A"]
    assert by_id[1]["old_total_cents"] == 100 and by_id[1]["new_total_cents"] == 110
    assert by_id[3]["old_codes"] == ["C", "D"] and by_id[3]["new_codes"] == ["C", "D"]
    assert by_id[3]["old_total_cents"] == 700 and by_id[3]["new_total_cents"] == 720

    summary = at_v1.get("/summary").json()
    assert summary["version"] == 1
    assert summary["years"] == [2023, 2024]
    assert {g["group_id"] for g in summary["groups"]} == {1, 2, 3}


def test_transitive_links_form_one_group(tmp_path):
    client = TestClient(create_app(str(tmp_path / "t.db")))
    put_base(
        client,
        [
            {"year": 2023, "code": "A", "amount_cents": 1},
            {"year": 2023, "code": "B", "amount_cents": 2},
            {"year": 2024, "code": "X", "amount_cents": 10},
            {"year": 2024, "code": "Y", "amount_cents": 20},
        ],
    )
    entries = [
        {"old_code": "A", "new_code": "X"},
        {"old_code": "B", "new_code": "X"},
        {"old_code": "B", "new_code": "Y"},
    ]
    assert post_mapping(client, 1, entries).status_code == 201
    groups = client.get("/summary").json()["groups"]
    assert len(groups) == 1
    (group,) = groups
    assert group["old_codes"] == ["A", "B"]
    assert group["new_codes"] == ["X", "Y"]
    assert group["old_total_cents"] == 3
    assert group["new_total_cents"] == 30


def test_split_group_carries_unchanged_and_diffs_the_rest(at_v1):
    r = post_mapping(at_v1, 2, V2_ENTRIES)
    assert r.status_code == 201
    record = r.json()
    assert record["replaced_version"] == 1
    assert record["removed_group_ids"] == [3]  # merged C/D group is taken down
    by_id = {g["group_id"]: g for g in record["summaries"]}
    assert set(by_id) == {4, 5}  # only new groups are written
    assert by_id[4]["old_codes"] == ["C"] and by_id[4]["old_total_cents"] == 300
    assert by_id[4]["new_codes"] == ["C"] and by_id[4]["new_total_cents"] == 310
    assert by_id[5]["old_codes"] == ["D"] and by_id[5]["new_total_cents"] == 410

    summary = at_v1.get("/summary").json()
    assert summary["version"] == 2
    # groups 1 and 2 kept their ids and totals; 4 and 5 are the split results
    assert {g["group_id"] for g in summary["groups"]} == {1, 2, 4, 5}
    assert at_v1.get("/changes/2").json() == record


def test_group_ids_are_never_reused(at_v1):
    assert post_mapping(at_v1, 2, V2_ENTRIES).status_code == 201
    r = post_mapping(at_v1, 3, V1_ENTRIES)  # re-create the old merged group
    assert r.status_code == 201
    record = r.json()
    assert record["replaced_version"] == 2
    assert record["removed_group_ids"] == [4, 5]
    (merged,) = record["summaries"]
    assert merged["group_id"] == 6  # id 3 is not recycled
    assert merged["old_codes"] == ["C", "D"]


# ------------------------------------------------------ replay / conflict
def test_same_version_replay_and_conflict(at_v1):
    original = at_v1.get("/changes/1").json()

    replay = post_mapping(at_v1, 1, V1_ENTRIES)
    assert replay.status_code == 200
    assert replay.json() == original  # the diff recorded at the time

    assert post_mapping(at_v1, 2, V2_ENTRIES).status_code == 201
    replay2 = post_mapping(at_v1, 2, V2_ENTRIES)
    assert replay2.status_code == 200
    assert replay2.json() == at_v1.get("/changes/2").json()

    # same version, different content -> conflict, state untouched
    tampered = V1_ENTRIES + [{"old_code": "D", "new_code": "C"}]
    assert post_mapping(at_v1, 1, tampered).status_code == 409
    assert post_mapping(at_v1, 2, tampered).status_code == 409
    assert at_v1.get("/summary").json()["version"] == 2


def test_stale_version_rejected(at_v1):
    assert post_mapping(at_v1, 5, V2_ENTRIES).status_code == 201  # gaps allowed
    assert post_mapping(at_v1, 3, V2_ENTRIES).status_code == 409  # 3 < current


# ------------------------------------------------------------- validation
def test_missing_base_code_rejects_whole_version(at_v1):
    entries = [e for e in V2_ENTRIES if not (e["old_code"] == "D")]
    r = post_mapping(at_v1, 2, entries)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "D" in detail["missing_old_codes"]
    assert "D" in detail["missing_new_codes"]
    # rejected as a whole: nothing changed, old supply still queryable
    assert at_v1.get("/summary").json()["version"] == 1
    versions = at_v1.get("/versions").json()
    assert versions["current_version"] == 1
    assert [v["version"] for v in versions["versions"]] == [1]
    assert at_v1.get("/changes/1").status_code == 200
    assert at_v1.get("/changes/2").status_code == 404


def test_unknown_code_rejected(at_v1):
    entries = V2_ENTRIES[:-1] + [{"old_code": "D", "new_code": "ZZ"}]
    r = post_mapping(at_v1, 2, entries)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["unknown_new_codes"] == ["ZZ"]
    assert "D" in detail["missing_new_codes"]
    assert at_v1.get("/summary").json()["version"] == 1


# -------------------------------------------------------------- restarts
def test_restart_keeps_versions_changes_and_summary(tmp_path):
    db = str(tmp_path / "restart.db")
    app1 = create_app(db)
    c1 = TestClient(app1)
    put_base(c1)
    post_mapping(c1, 1, V1_ENTRIES)
    post_mapping(c1, 2, V2_ENTRIES)
    changes1 = c1.get("/changes/1").json()
    changes2 = c1.get("/changes/2").json()
    app1.state.store.close()

    c2 = TestClient(create_app(db))  # fresh app over the same file
    assert c2.get("/changes/1").json() == changes1
    assert c2.get("/changes/2").json() == changes2
    summary = c2.get("/summary").json()
    assert summary["version"] == 2
    assert {g["group_id"] for g in summary["groups"]} == {1, 2, 4, 5}
    assert c2.get("/versions").json()["current_version"] == 2

    # replay still works after restart; a new version builds on v2
    assert post_mapping(c2, 1, V1_ENTRIES).status_code == 200
    r = post_mapping(c2, 3, V1_ENTRIES)
    assert r.status_code == 201
    assert r.json()["replaced_version"] == 2


# ------------------------------------------------------------- concurrency
def test_concurrent_same_version_one_wins(seeded):
    other = [dict(e) for e in V2_ENTRIES]
    other[0] = {"old_code": "A", "new_code": "B"}  # different valid full mapping
    other[1] = {"old_code": "B", "new_code": "A"}

    barrier = threading.Barrier(2)
    results = []

    def worker(entries):
        client = TestClient(seeded.app)
        barrier.wait()
        results.append(
            client.post("/mappings", json={"version": 2, "entries": entries}).status_code
        )

    t1 = threading.Thread(target=worker, args=(V2_ENTRIES,))
    t2 = threading.Thread(target=worker, args=(other,))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert sorted(results) == [201, 409]
    assert [v["version"] for v in seeded.get("/versions").json()["versions"]] == [2]


def test_concurrent_next_versions_apply_against_actual_predecessor(at_v1):
    barrier = threading.Barrier(2)
    outcomes = {}

    def worker(name, version, entries):
        client = TestClient(at_v1.app)
        barrier.wait()
        r = client.post("/mappings", json={"version": version, "entries": entries})
        outcomes[name] = r.status_code

    t1 = threading.Thread(target=worker, args=("v2", 2, V2_ENTRIES))
    t2 = threading.Thread(target=worker, args=("v3", 3, V1_ENTRIES))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    versions = at_v1.get("/versions").json()
    if outcomes == {"v2": 201, "v3": 201}:
        # v2 committed first; v3's diff is based on the version it replaced
        assert versions["current_version"] == 3
        assert at_v1.get("/changes/3").json()["replaced_version"] == 2
        assert at_v1.get("/changes/2").json()["replaced_version"] == 1
    else:
        # v3 committed first (replacing v1); v2 then loses as stale
        assert outcomes == {"v2": 409, "v3": 201}
        assert versions["current_version"] == 3
        assert at_v1.get("/changes/3").json()["replaced_version"] == 1
        assert at_v1.get("/changes/2").status_code == 404


# ------------------------------------------------------------------ misc
def test_get_mapping_and_versions(at_v1):
    mapping = at_v1.get("/mappings/1").json()
    assert mapping["version"] == 1
    assert {tuple(sorted(e.values())) for e in mapping["entries"]} == {
        (e["old_code"], e["new_code"]) for e in V1_ENTRIES
    }
    assert at_v1.get("/mappings/99").status_code == 404
    assert at_v1.get("/changes/99").status_code == 404
    versions = at_v1.get("/versions").json()
    assert versions["current_version"] == 1
    assert [v["version"] for v in versions["versions"]] == [1]
