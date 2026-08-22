"""LegacyCore FastAPI application: routes, scenario injection, and rendering.

Server-rendered navigation (POST -> 303 -> GET) with a single iframe workspace boundary,
matching the interaction style of legacy employee/core-banking screens.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .data import find_member
from .scenarios import SCENARIO_COOKIE, SLOW_DELAY_SECONDS, Scenario, resolve_scenario

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
    member = find_member(member_number)
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


@app.get("/workspace/member/{member_number}", response_class=HTMLResponse)
async def member_profile(request: Request, member_number: str) -> Response:
    scenario = _request_scenario(request)
    if scenario is Scenario.SLOW:
        await asyncio.sleep(SLOW_DELAY_SECONDS)
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
    profile_context: dict[str, Any] = {
        "member": member,
        "scenario_suffix": _suffix(scenario),
        "show_dialog": scenario is Scenario.UNEXPECTED_DIALOG,
    }
    return _TEMPLATES.TemplateResponse(
        request=request, name="profile.html", context=profile_context
    )
