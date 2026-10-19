"""Раздел «Ошибки» — что ломалось у ботов и у каких аккаунтов. Только админ."""

from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from web.auth import require_admin
from web.db import get_db
from web.models import User
from web.services.errors import ERROR_TITLES, KEEP_DAYS, PAGE_SIZE, count, feed, summary
from web.services.providers import list_providers
from web.templating import render

router = APIRouter(prefix="/errors", tags=["errors"])


@router.get("", response_class=HTMLResponse)
def errors_page(
    request: Request,
    provider: str = "",
    key: str = "",
    page: str = "",
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    provider_id = int(provider) if provider.isdigit() else None
    key = key if key in ERROR_TITLES else ""

    # Номер страницы — строкой: мусор в адресе даёт первую страницу, а не 422.
    total = count(db, provider_id=provider_id, key=key)
    pages = max(1, -(-total // PAGE_SIZE))
    current = min(max(1, int(page) if page.isdigit() else 1), pages)

    # Фильтр в ссылках страниц: иначе переход на вторую страницу сбрасывал бы его.
    filters = urlencode({k: v for k, v in (("provider", provider_id or ""), ("key", key)) if v})

    return render(
        request,
        "errors.html",
        user,
        summary=summary(db),
        events=feed(db, provider_id=provider_id, key=key,
                    limit=PAGE_SIZE, offset=(current - 1) * PAGE_SIZE),
        total=total,
        page=current,
        pages=pages,
        page_size=PAGE_SIZE,
        page_url="/errors?" + (filters + "&" if filters else "") + "page=",
        providers=list_providers(db),
        titles=ERROR_TITLES,
        chosen_provider=provider_id,
        chosen_key=key,
        keep_days=KEEP_DAYS,
    )
