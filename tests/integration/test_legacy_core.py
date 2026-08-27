import pytest
from fastapi.testclient import TestClient

from legacy_core import mutations
from legacy_core.app import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def _reset_mutations() -> None:
    mutations.reset()


def test_shell_serves_with_single_iframe_and_context() -> None:
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.text
    assert body.count("<iframe") == 1
    assert "Branch: 014" in body
    assert "TELLER04" in body
    assert "BR014-03" in body
    assert "Tool 200" in body


def test_inquiry_form_served() -> None:
    resp = client.get("/workspace/inquiry")
    assert resp.status_code == 200
    body = resp.text
    assert "Member Number" in body
    assert 'name="member_number"' in body
    assert 'type="submit"' in body


def test_member_found_shows_profile_and_savings_row() -> None:
    resp = client.post("/workspace/inquiry", data={"member_number": "12345"})
    assert resp.status_code == 200  # TestClient follows the 303 redirect
    body = resp.text
    assert "ALICE EXAMPLE" in body
    assert "Share Savings" in body
    assert "<td>00</td>" in body  # savings suffix
    assert "8,421.31" in body  # current balance (extraction target)
    assert "Available" in body
    assert "OPEN" in body


def test_second_member_has_different_savings_balance() -> None:
    resp = client.post("/workspace/inquiry", data={"member_number": "54321"})
    assert resp.status_code == 200
    assert "312.45" in resp.text
    assert "8,421.31" not in resp.text


def test_unknown_member_is_data_driven_not_found() -> None:
    resp = client.post("/workspace/inquiry", data={"member_number": "99999"})
    assert resp.status_code == 200
    assert "Member record not found" in resp.text
    assert "Share Savings" not in resp.text


def test_unexpected_dialog_scenario_renders_modal() -> None:
    normal = client.get("/workspace/member/12345")
    assert "System Notice" not in normal.text
    dialog = client.get("/workspace/member/12345", params={"scenario": "unexpected_dialog"})
    assert dialog.status_code == 200
    assert "System Notice" in dialog.text
    assert 'role="dialog"' in dialog.text


def test_verification_scenario_withholds_details_behind_a_code() -> None:
    resp = client.get("/workspace/member/12345", params={"scenario": "verification_required"})
    assert resp.status_code == 200
    body = resp.text
    assert "Identity Verification Required" in body
    assert "Employee Verification Code" in body
    assert "8,421.31" not in body  # the balance is not released yet


def test_verification_wrong_code_is_rejected() -> None:
    resp = client.post(
        "/workspace/member/12345",
        params={"scenario": "verification_required"},
        data={"verification_code": "0000"},
    )
    assert resp.status_code == 200
    assert "Invalid verification code" in resp.text
    assert "8,421.31" not in resp.text


def test_verification_correct_code_releases_the_profile() -> None:
    # TestClient follows the 303; the set cookie carries verification to the profile GET.
    resp = client.post(
        "/workspace/member/12345",
        params={"scenario": "verification_required"},
        data={"verification_code": "4729"},
    )
    assert resp.status_code == 200
    assert "8,421.31" in resp.text  # verified: the balance is now shown
    assert "Identity Verification Required" not in resp.text


def test_slow_scenario_still_serves_correctly() -> None:
    resp = client.get("/workspace/member/12345", params={"scenario": "slow"})
    assert resp.status_code == 200
    assert "Share Savings" in resp.text


def test_accounts_use_table_without_test_ids() -> None:
    body = client.get("/workspace/member/12345").text
    assert "<table" in body
    assert "data-testid" not in body
    assert "data-test" not in body


def test_one_link_styled_as_button_and_search_is_real_button() -> None:
    profile = client.get("/workspace/member/12345").text
    assert "lc-linkbtn" in profile  # the intentional link-as-button (History)
    inquiry = client.get("/workspace/inquiry").text
    assert "<button" in inquiry  # Search is a real button


def test_sidebar_exposes_only_member_inquiry_as_route() -> None:
    body = client.get("/").text
    # Member Inquiry is the only functioning sidebar route.
    assert 'target="lc-workspace"' in body
    assert ">Member Inquiry</a>" in body
    # No misleading dead-end links: unavailable sections are non-interactive.
    assert 'href="#"' not in body
    assert 'aria-disabled="true"' in body
    for section in ["Accounts", "Loans", "Cards", "Notes", "Maintenance", "Teller", "Admin"]:
        assert f">{section}</span>" in body


def test_sub_account_review_and_normal_commit() -> None:
    review = client.get("/workspace/member/12345/sub-account").text
    assert "Open Sub-Account" in review and "Create Account" in review
    created = client.post("/workspace/member/12345/sub-account")
    assert "Sub-account created" in created.text
    assert mutations.commit_dispatch_count() == 1
    assert "Share Savings Sub" in client.get("/workspace/member/12345").text


def test_sub_account_second_commit_is_rejected() -> None:
    client.post("/workspace/member/12345/sub-account")
    second = client.post("/workspace/member/12345/sub-account")
    assert "A sub-account of this type already exists" in second.text
    assert mutations.commit_dispatch_count() == 2  # dispatch counted, but not a second create


def test_commit_then_timeout_commits_before_withholding() -> None:
    # The mutation is applied even though the completion signal is ambiguous.
    resp = client.post(
        "/workspace/member/12345/sub-account", params={"scenario": "commit_ambiguous"}
    )
    assert "The request was interrupted" in resp.text
    assert mutations.has_sub_account("12345")  # committed despite the ambiguous page


def test_commit_dropped_does_not_commit() -> None:
    resp = client.post("/workspace/member/12345/sub-account", params={"scenario": "commit_dropped"})
    assert "The request was interrupted" in resp.text
    assert not mutations.has_sub_account("12345")


def test_unexpected_dialog_makes_underlying_content_inert() -> None:
    normal = client.get("/workspace/member/12345").text
    assert '<section class="gc2">' in normal
    assert "inert" not in normal
    dialog = client.get(
        "/workspace/member/12345", params={"scenario": "unexpected_dialog"}
    ).text
    # The notice genuinely owns interaction: underlying profile content is inert
    # (removed from tab order and the accessibility tree) until acknowledged.
    assert '<section class="gc2" inert>' in dialog


def test_reset_clears_demo_state() -> None:
    mutations.create_sub_account("12345")
    assert mutations.has_sub_account("12345")
    resp = client.post("/reset")
    assert resp.status_code == 200
    assert resp.json() == {"reset": True}
    assert not mutations.has_sub_account("12345")
