"""Раздел «Ошибки» — что ломалось у ботов и у каких аккаунтов. Только админ."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from web.auth import require_admin
from web.db import get_db
from web.models import User
from web.services.errors import ERROR_TITLES, KEEP_DAYS, feed, summary
from web.services.providers import list_providers
from web.templating import render

router = APIRouter(prefix="/errors", tags=["errors"])


@router.get("", response_class=HTMLResponse)
def errors_page(
    request: Request,
    provider: str = "",
    key: str = "",
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    provider_id = int(provider) if provider.isdigit() else None
    key = key if key in ERROR_TITLES else ""

    return render(
        request,
        "errors.html",
        user,
        summary=summary(db),
        events=feed(db, provider_id=provider_id, key=key),
        providers=list_providers(db),
        titles=ERROR_TITLES,
        chosen_provider=provider_id,
        chosen_key=key,
        keep_days=KEEP_DAYS,
    )
