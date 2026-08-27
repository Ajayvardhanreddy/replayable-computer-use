"""LegacyCore FastAPI application: routes, scenario injection, and rendering.

Server-rendered navigation (POST -> 303 -> GET) with a single iframe workspace boundary,
matching the interaction style of legacy employee/core-banking screens.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import mutations
from .data import find_member
from .scenarios import (
    COMMIT_HOLD_SECONDS,
    EMPLOYEE_VERIFICATION_CODE,
    SCENARIO_COOKIE,
    SLOW_DELAY_SECONDS,
    VERIFIED_COOKIE,
    Scenario,
    resolve_scenario,
)

_AMBIGUOUS_COMMIT = frozenset(
    {Scenario.COMMIT_THEN_TIMEOUT, Scenario.COMMIT_AMBIGUOUS, Scenario.COMMIT_UNVERIFIABLE}
)

_BASE_DIR = Path(__file__).parent
_TEMPLATES = Jinja2Templates(directory=str(_BASE_DIR / "templates"))

# Static synthetic workstation context shown in the shell chrome.
_BRANCH = "014"
_USER = "TELLER04"
_WORKSTATION = "BR014-03"
_TOOL_NUMBER = "200"

app = FastAPI(title="LegacyCore Member Service", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(_BASE_DIR / "static")), name="static")


def _request_scenario(request: Request) -> Scenario:
    return resolve_scenario(
        request.query_params.get("scenario"),
        request.cookies.get(SCENARIO_COOKIE),
    )


@app.post("/reset")
async def reset_demo_state() -> Response:
    """Clear LegacyCore's in-memory demo state (created sub-accounts, acknowledgements, and the
    dispatch counter). A demo/dev convenience so the mutation demos can be re-run without
    restarting the server; the synthetic app has no real data to protect."""
    mutations.reset()
    return JSONResponse({"reset": True})


def _suffix(scenario: Scenario) -> str:
    return "" if scenario is Scenario.NORMAL else f"?scenario={scenario.value}"


@app.get("/", response_class=HTMLResponse)
async def shell(request: Request) -> Response:
    scenario = _request_scenario(request)
    context: dict[str, Any] = {
        "branch": _BRANCH,
        "user": _USER,
        "workstation": _WORKSTATION,
        "tool_number": _TOOL_NUMBER,
        "scenario_suffix": _suffix(scenario),
    }
    response = _TEMPLATES.TemplateResponse(request=request, name="shell.html", context=context)
    # Persist an explicitly requested scenario so it survives the iframe navigation.
    if request.query_params.get("scenario") is not None:
        response.set_cookie(SCENARIO_COOKIE, scenario.value)
    return response


@app.get("/workspace/inquiry", response_class=HTMLResponse)
async def inquiry(request: Request) -> Response:
    scenario = _request_scenario(request)
    context: dict[str, Any] = {
        "scenario_suffix": _suffix(scenario),
        "not_found": False,
        "member_number": "",
    }
    return _TEMPLATES.TemplateResponse(request=request, name="inquiry.html", context=context)


@app.post("/workspace/inquiry")
async def submit_inquiry(
    request: Request,
    member_number: str = Form(default=""),
    last_name: str = Form(default=""),
) -> Response:
    scenario = _request_scenario(request)
    member = None if scenario is Scenario.NOT_FOUND else find_member(member_number)
    if member is None:
        context: dict[str, Any] = {
            "scenario_suffix": _suffix(scenario),
            "not_found": True,
            "member_number": member_number,
        }
        return _TEMPLATES.TemplateResponse(
            request=request, name="inquiry.html", context=context, status_code=200
        )
    return RedirectResponse(
        f"/workspace/member/{member.member_number}{_suffix(scenario)}", status_code=303
    )


def _is_verified(request: Request, member_number: str) -> bool:
    return request.cookies.get(VERIFIED_COOKIE) == member_number


@app.get("/workspace/member/{member_number}", response_class=HTMLResponse)
async def member_profile(request: Request, member_number: str) -> Response:
    scenario = _request_scenario(request)
    if scenario is Scenario.SLOW:
        await asyncio.sleep(SLOW_DELAY_SECONDS)
    # A no-longer-valid session or an unauthorized read returns a recognizable state instead of
    # the expected profile; deterministic replay then observes "expected != observed" and stops
    # with a typed failure plus safe evidence, without any app-specific runtime classification.
    if scenario is Scenario.SESSION_EXPIRED:
        return _TEMPLATES.TemplateResponse(
            request=request, name="session_expired.html",
            context={"scenario_suffix": _suffix(scenario)}, status_code=200,
        )
    if scenario is Scenario.PERMISSION_DENIED:
        return _TEMPLATES.TemplateResponse(
            request=request, name="access_denied.html",
            context={"scenario_suffix": _suffix(scenario)}, status_code=200,
        )
    member = None if scenario is Scenario.NOT_FOUND else find_member(member_number)
    if member is None:
        not_found_context: dict[str, Any] = {
            "scenario_suffix": _suffix(scenario),
            "not_found": True,
            "member_number": member_number,
        }
        return _TEMPLATES.TemplateResponse(
            request=request, name="inquiry.html", context=not_found_context, status_code=200
        )
    # A flagged account withholds its details behind manual employee verification. Once
    # an authorized employee has verified (recorded in a cookie), the profile is shown.
    if scenario is Scenario.VERIFICATION_REQUIRED and not _is_verified(request, member_number):
        verify_context: dict[str, Any] = {
            "member_number": member_number,
            "scenario_suffix": _suffix(scenario),
            "error": False,
        }
        return _TEMPLATES.TemplateResponse(
            request=request, name="verification.html", context=verify_context
        )
    # The profile is the independent read path used to verify a mutation. Under
    # COMMIT_UNVERIFIABLE it cannot render once a commit has happened, so the read-back
    # after the commit cannot establish the effect (reads before the commit are normal).
    if scenario is Scenario.COMMIT_UNVERIFIABLE and mutations.has_sub_account(member_number):
        return _TEMPLATES.TemplateResponse(
            request=request, name="read_interrupted.html", context={}, status_code=200
        )
    # A recoverable post-commit block: the independent read-back is gated behind an
    # unexpected dialog until an operator acknowledges it (then it renders normally).
    acked = mutations.is_acknowledged(member_number)
    verification_dialog = (
        scenario is Scenario.VERIFICATION_DIALOG
        and mutations.has_sub_account(member_number)
        and not acked
    )
    show_dialog = scenario is Scenario.UNEXPECTED_DIALOG or verification_dialog
    ack_href = (
        f"/workspace/member/{member_number}/ack{_suffix(scenario)}"
        if verification_dialog
        else f"/workspace/member/{member_number}?scenario=normal"
    )
    profile_context: dict[str, Any] = {
        "member": member,
        "scenario_suffix": _suffix(scenario),
        "show_dialog": show_dialog,
        "ack_href": ack_href,
        "sub_accounts": mutations.created_sub_accounts(member_number),
    }
    return _TEMPLATES.TemplateResponse(
        request=request, name="profile.html", context=profile_context
    )


@app.get("/workspace/member/{member_number}/ack", response_class=HTMLResponse)
async def acknowledge_verification(request: Request, member_number: str) -> Response:
    """Record that an operator acknowledged the post-commit verification dialog."""
    scenario = _request_scenario(request)
    mutations.acknowledge_verification(member_number)
    return RedirectResponse(
        f"/workspace/member/{member_number}{_suffix(scenario)}", status_code=303
    )


@app.get("/workspace/member/{member_number}/sub-account", response_class=HTMLResponse)
async def sub_account_form(request: Request, member_number: str) -> Response:
    scenario = _request_scenario(request)
    if find_member(member_number) is None:
        return _TEMPLATES.TemplateResponse(
            request=request,
            name="inquiry.html",
            context={"scenario_suffix": _suffix(scenario), "not_found": True,
                     "member_number": member_number},
            status_code=200,
        )
    return _TEMPLATES.TemplateResponse(
        request=request,
        name="sub_account.html",
        context={"state": "review", "member_number": member_number,
                 "label": mutations.SUB_ACCOUNT_LABEL, "scenario_suffix": _suffix(scenario)},
    )


@app.post("/workspace/member/{member_number}/sub-account")
async def open_sub_account(request: Request, member_number: str) -> Response:
    """Commit a new sub-account. Every invocation is counted so a test can prove an
    uncertain mutation is dispatched exactly once."""
    scenario = _request_scenario(request)
    if find_member(member_number) is None:
        return _TEMPLATES.TemplateResponse(
            request=request,
            name="inquiry.html",
            context={"scenario_suffix": _suffix(scenario), "not_found": True,
                     "member_number": member_number},
            status_code=200,
        )
    mutations.record_commit_dispatch()

    def _page(state: str, status: int = 200) -> Response:
        return _TEMPLATES.TemplateResponse(
            request=request,
            name="sub_account.html",
            context={"state": state, "member_number": member_number,
                     "label": mutations.SUB_ACCOUNT_LABEL, "scenario_suffix": _suffix(scenario)},
            status_code=status,
        )

    if mutations.has_sub_account(member_number):
        return _page("rejected")  # explicit application rejection: already exists
    if scenario is Scenario.COMMIT_DROPPED:
        return _page("pending")  # dispatched but not committed; ambiguous signal
    # These scenarios genuinely commit, then withhold a normal completion signal.
    mutations.create_sub_account(member_number)
    if scenario is Scenario.COMMIT_THEN_TIMEOUT:
        await asyncio.sleep(COMMIT_HOLD_SECONDS)  # real timeout: response withheld
        return _page("pending")
    if scenario in _AMBIGUOUS_COMMIT:
        return _page("pending")
    return _page("created")  # normal: a clear success confirmation


@app.post("/workspace/member/{member_number}")
async def verify_member(
    request: Request, member_number: str, verification_code: str = Form(default="")
) -> Response:
    scenario = _request_scenario(request)
    member = find_member(member_number)
    if member is None:
        not_found_context: dict[str, Any] = {
            "scenario_suffix": _suffix(scenario),
            "not_found": True,
            "member_number": member_number,
        }
        return _TEMPLATES.TemplateResponse(
            request=request, name="inquiry.html", context=not_found_context, status_code=200
        )
    if verification_code.strip() == EMPLOYEE_VERIFICATION_CODE:
        # Verified: record it and return to the profile, which now renders normally.
        response: Response = RedirectResponse(
            f"/workspace/member/{member_number}{_suffix(scenario)}", status_code=303
        )
        response.set_cookie(VERIFIED_COOKIE, member_number)
        return response
    error_context: dict[str, Any] = {
        "member_number": member_number,
        "scenario_suffix": _suffix(scenario),
        "error": True,
    }
    return _TEMPLATES.TemplateResponse(
        request=request, name="verification.html", context=error_context, status_code=200
    )
