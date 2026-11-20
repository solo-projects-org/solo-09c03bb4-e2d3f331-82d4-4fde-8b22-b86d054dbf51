"""End-to-end tests for verification forms and vouchers."""
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
V1_ENTRIES = [
    {"old_code": "A", "new_code": "A"},
    {"old_code": "B", "new_code": "B"},
    {"old_code": "C", "new_code": "C"},
    {"old_code": "C", "new_code": "D"},
    {"old_code": "D", "new_code": "D"},
]
V2_ENTRIES = [{"old_code": c, "new_code": c} for c in "ABCD"]


def put_base(client, amounts=BASE_AMOUNTS):
    return client.put("/base", json={"amounts": amounts})


def post_mapping(client, version, entries):
    return client.post("/mappings", json={"version": version, "entries": entries})


def register(client, form_no, year, codes, total):
    return client.post(
        "/forms",
        json={
            "form_no": form_no,
            "year": year,
            "tax_codes": codes,
            "total_cents": total,
        },
    )


def revoke(client, form_no):
    return client.post(f"/forms/{form_no}/revoke")


def issue(client, observed_version, group_id, year, form_nos, preview=False):
    path = "/vouchers/preview" if preview else "/vouchers"
    return client.post(
        path,
        json={
            "observed_version": observed_version,
            "group_id": group_id,
            "year": year,
            "form_nos": form_nos,
        },
    )


@pytest.fixture()
def at_v1(tmp_path):
    client = TestClient(create_app(str(tmp_path / "test.db")))
    assert put_base(client).status_code == 200
    assert post_mapping(client, 1, V1_ENTRIES).status_code == 201
    return client


# ------------------------------------------------------------- registration
def test_register_requires_base(tmp_path):
    client = TestClient(create_app(str(tmp_path / "t.db")))
    r = register(client, "F1", 2023, ["A"], 100)
    assert r.status_code == 409


def test_register_validates_against_base_table(at_v1):
    ok = register(at_v1, "F1", 2023, ["A", "B"], 300)  # 100 + 200
    assert ok.status_code == 201
    body = ok.json()
    assert body["status"] == "active" and body["revoked_at"] is None
    assert body["tax_codes"] == ["A", "B"]

    bad_total = register(at_v1, "F2", 2023, ["A"], 999)
    assert bad_total.status_code == 422
    assert bad_total.json()["detail"]["expected_total_cents"] == 100

    unknown = register(at_v1, "F3", 2023, ["ZZ"], 1)
    assert unknown.status_code == 422
    assert unknown.json()["detail"]["unknown_tax_codes"] == ["ZZ"]

    dup_code = register(at_v1, "F4", 2023, ["A", "A"], 200)
    assert dup_code.status_code == 422

    bad_year = register(at_v1, "F5", 1999, ["A"], 100)
    assert bad_year.status_code == 422

    duplicate_no = register(at_v1, "F1", 2024, ["A"], 110)
    assert duplicate_no.status_code == 409

    # nothing was written by the rejected registrations
    nos = [f["form_no"] for f in at_v1.get("/forms").json()["forms"]]
    assert nos == ["F1"]


def test_revocation_keeps_the_row_and_is_idempotent(at_v1):
    assert register(at_v1, "F1", 2023, ["A"], 100).status_code == 201
    r = revoke(at_v1, "F1")
    assert r.status_code == 200
    assert r.json()["status"] == "revoked" and r.json()["revoked_at"]
    assert at_v1.get("/forms/F1").json()["status"] == "revoked"
    assert [f["form_no"] for f in at_v1.get("/forms").json()["forms"]] == ["F1"]
    assert at_v1.get("/forms?status=active").json()["forms"] == []
    assert [f["form_no"] for f in at_v1.get("/forms?status=revoked").json()["forms"]] == [
        "F1"
    ]
    # repeat revocation is idempotent
    assert revoke(at_v1, "F1").json()["status"] == "revoked"
    assert revoke(at_v1, "MISSING").status_code == 404


# ----------------------------------------------------------------- selection
def _seed_cd_forms(client, year):
    """v1 group 3 = {C, D}; amounts 300/400 in 2023 and 310/410 in 2024."""
    c, d = (300, 400) if year == 2023 else (310, 410)
    assert register(client, "F-C", year, ["C"], c).status_code == 201
    assert register(client, "F-D", year, ["D"], d).status_code == 201
    assert register(client, "F-CD", year, ["C", "D"], c + d).status_code == 201


def test_issue_picks_fewest_forms_and_covers_exactly(at_v1):
    _seed_cd_forms(at_v1, 2023)
    # three candidates: F-C + F-D (2 forms) or F-CD (1 form) -> the single wins
    r = issue(at_v1, 1, 3, 2023, ["F-C", "F-D", "F-CD"])
    assert r.status_code == 201
    body = r.json()
    assert body["voucher_no"] == "V000001"
    assert body["version"] == 1
    assert body["selected_form_nos"] == ["F-CD"]
    assert body["total_cents"] == 700
    assert at_v1.get(f"/vouchers/{body['voucher_no']}").json() == body
    assert at_v1.get("/vouchers").json()["vouchers"][0]["voucher_no"] == "V000001"


def test_issue_splits_when_no_single_form_covers(at_v1):
    register(at_v1, "F-C", 2023, ["C"], 300)
    register(at_v1, "F-D", 2023, ["D"], 400)
    r = issue(at_v1, 1, 3, 2023, ["F-C", "F-D"])
    assert r.status_code == 201
    body = r.json()
    assert body["selected_form_nos"] == ["F-C", "F-D"]
    assert body["total_cents"] == 700  # equals the group's 2023 total


def test_cover_search_does_not_depend_on_submission_order(at_v1):
    # Regression: earlier DFS cursor skipped later branches whose representative
    # code sorted before the cursor position.  Reverse order must still cover.
    assert register(at_v1, "Z-D", 2023, ["D"], 400).status_code == 201
    assert register(at_v1, "A-C", 2023, ["C"], 300).status_code == 201
    r = issue(at_v1, 1, 3, 2023, ["Z-D", "A-C"])
    assert r.status_code == 201
    assert r.json()["selected_form_nos"] == ["A-C", "Z-D"]
    assert r.json()["total_cents"] == 700


def test_form_with_code_outside_group_is_used_whole_or_rejected(at_v1):
    # F-ABC contains codes A,B outside group 3 {C,D}: forms are used whole,
    # so it must not be masked down to {C}; no exact cover is possible.
    register(at_v1, "F-ABC", 2023, ["A", "B", "C"], 600)
    register(at_v1, "F-D", 2023, ["D"], 400)
    r = issue(at_v1, 1, 3, 2023, ["F-ABC", "F-D"])
    assert r.status_code == 422
    # the exact same form is selectable once the target group contains all
    # of its codes (group 1 is {A}; a form covering just A covers it whole)
    register(at_v1, "F-A", 2023, ["A"], 100)
    r = issue(at_v1, 1, 1, 2023, ["F-A", "F-ABC"])
    assert r.status_code == 201
    assert r.json()["selected_form_nos"] == ["F-A"]  # F-ABC excluded, F-A covers


def test_other_year_uses_that_year_total(at_v1):
    _seed_cd_forms(at_v1, 2024)
    r = issue(at_v1, 1, 3, 2024, ["F-CD"])
    assert r.status_code == 201
    assert r.json()["total_cents"] == 720


def test_overlapping_forms_cannot_cover(at_v1):
    register(at_v1, "F-CD1", 2023, ["C", "D"], 700)
    register(at_v1, "F-CD2", 2023, ["C", "D"], 700)  # same codes -> overlap
    r = issue(at_v1, 1, 3, 2023, ["F-CD1", "F-CD2"])
    # one of them alone covers exactly, so the minimum cover is a single form
    assert r.status_code == 201
    assert r.json()["selected_form_nos"] == ["F-CD1"]  # lex tie-break

    # genuinely impossible: every form misses D but overlaps on C
    register(at_v1, "F-C1", 2023, ["C"], 300)
    register(at_v1, "F-C2", 2023, ["C"], 300)
    r = issue(at_v1, 1, 3, 2023, ["F-C1", "F-C2"])
    assert r.status_code == 422
    assert "exactly cover" in r.json()["detail"]["message"]


def test_issue_input_validation(at_v1):
    register(at_v1, "F-C", 2023, ["C"], 300)

    assert issue(at_v1, 1, 999, 2023, ["F-C"]).status_code == 422  # unknown group
    assert issue(at_v1, 1, 3, 2024, ["F-C"]).status_code == 422  # wrong year form
    assert issue(at_v1, 1, 3, 2023, ["NOPE"]).status_code == 422  # unknown form
    assert issue(
        at_v1, 1, 3, 2023, ["F-C", "F-C"]
    ).status_code == 422  # duplicate form no
    # > 20 forms rejected by the request schema
    r = issue(at_v1, 1, 3, 2023, [f"F{i}" for i in range(21)])
    assert r.status_code == 422


def test_version_replacement_conflicts_and_no_voucher_written(at_v1):
    _seed_cd_forms(at_v1, 2023)
    # switch version while the caller still "sees" v1 (allowed during selection)
    assert post_mapping(at_v1, 2, V2_ENTRIES).status_code == 201
    # group 3 was removed by v2, so resolving the group fails first (422);
    # the stale-version 409 path is exercised below against a still-existing group
    assert issue(at_v1, 1, 3, 2023, ["F-CD"]).status_code == 422
    assert at_v1.get("/vouchers").json()["vouchers"] == []

    # re-create the merged group as a fresh group id under v3 and switch again:
    # now the group exists, but the caller's observed v2 is stale -> 409
    r3 = post_mapping(at_v1, 3, V1_ENTRIES)
    assert r3.status_code == 201
    merged_group = r3.json()["summaries"][0]["group_id"]
    r = issue(at_v1, 2, merged_group, 2023, ["F-CD"])
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["observed_version"] == 2 and detail["current_version"] == 3
    assert at_v1.get("/vouchers").json()["vouchers"] == []

    # issuing against the current version works and stamps the current version
    r = issue(at_v1, 3, merged_group, 2023, ["F-CD"])
    assert r.status_code == 201
    assert r.json()["version"] == 3


def test_revoked_unselected_form_does_not_block_issuance(at_v1):
    _seed_cd_forms(at_v1, 2023)
    assert revoke(at_v1, "F-C").status_code == 200
    assert revoke(at_v1, "F-D").status_code == 200
    # both revoked forms are unneeded while F-CD alone covers the group
    r = issue(at_v1, 1, 3, 2023, ["F-CD", "F-C", "F-D"])
    assert r.status_code == 201
    assert r.json()["selected_form_nos"] == ["F-CD"]
    assert r.json()["ignored_revoked_form_nos"] == ["F-C", "F-D"]
    assert at_v1.get("/vouchers/V000001").status_code == 200


def test_revoked_needed_form_makes_assembly_impossible_conflict(at_v1):
    register(at_v1, "F-C", 2023, ["C"], 300)
    register(at_v1, "F-D", 2023, ["D"], 400)
    assert revoke(at_v1, "F-D").status_code == 200
    # only F-C remains active -> cannot cover D; revoked form reported as conflict
    r = issue(at_v1, 1, 3, 2023, ["F-C", "F-D"])
    assert r.status_code == 409
    assert r.json()["detail"]["revoked_form_nos"] == ["F-D"]
    assert at_v1.get("/vouchers").json()["vouchers"] == []


def test_revoking_selected_form_between_selection_and_write_conflicts(at_v1):
    """Simulate the check window: selected forms are re-read in the write txn."""
    _seed_cd_forms(at_v1, 2023)
    preview = issue(at_v1, 1, 3, 2023, ["F-CD"], preview=True)
    assert preview.status_code == 200
    assert preview.json()["selected_form_nos"] == ["F-CD"]
    assert "voucher_no" not in preview.json()

    assert revoke(at_v1, "F-CD").status_code == 200
    r = issue(at_v1, 1, 3, 2023, ["F-CD"])
    assert r.status_code == 409
    assert r.json()["detail"]["revoked_form_nos"] == ["F-CD"]
    assert at_v1.get("/vouchers").json()["vouchers"] == []


def test_concurrent_revocation_and_issuance_one_outcome(at_v1):
    _seed_cd_forms(at_v1, 2023)
    barrier = threading.Barrier(2)
    outcomes = []

    def issuer():
        client = TestClient(at_v1.app)
        barrier.wait()
        outcomes.append(
            client.post(
                "/vouchers",
                json={
                    "observed_version": 1,
                    "group_id": 3,
                    "year": 2023,
                    "form_nos": ["F-CD"],
                },
            ).status_code
        )

    def revoker():
        client = TestClient(at_v1.app)
        barrier.wait()
        outcomes.append(client.post("/forms/F-CD/revoke").status_code)

    t1 = threading.Thread(target=issuer)
    t2 = threading.Thread(target=revoker)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    # revocation always succeeds (200); issuance gets 201 or 409 depending on order
    assert 200 in outcomes
    vouchers = at_v1.get("/vouchers").json()["vouchers"]
    if 201 in outcomes:
        assert len(vouchers) == 1
        assert vouchers[0]["selected_form_nos"] == ["F-CD"]
    else:
        assert outcomes == [409, 200] or outcomes == [200, 409]
        assert vouchers == []


def test_vouchers_and_forms_survive_restart(tmp_path):
    db = str(tmp_path / "restart.db")
    app = create_app(db)
    c1 = TestClient(app)
    put_base(c1)
    post_mapping(c1, 1, V1_ENTRIES)
    register(c1, "F-CD", 2023, ["C", "D"], 700)
    r = issue(c1, 1, 3, 2023, ["F-CD"])
    assert r.status_code == 201
    voucher = r.json()
    app.state.store.close()

    c2 = TestClient(create_app(db))
    assert c2.get("/vouchers/V000001").json() == voucher
    assert c2.get("/forms/F-CD").json()["form_no"] == "F-CD"
    # the voucher sequence continues rather than restarting
    register(c2, "F-C", 2023, ["C"], 300)
    register(c2, "F-D", 2023, ["D"], 400)
    revoke(c2, "F-CD")
    r = issue(c2, 1, 3, 2023, ["F-C", "F-D"])
    assert r.status_code == 201
    assert r.json()["voucher_no"] == "V000002"
