"""Local-only React interface and JSON operations for the shared library.

The shared service owns extraction, provider selection, and metered processing.
Paid searches and updates require protected explicit requests. Source content
is serialized as data for the React source renderer, never executable markup.
"""
from __future__ import annotations

import hmac
from html import escape
import json
import logging
from pathlib import Path
import re
import secrets
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, quote, urlencode, urlsplit

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from .ui_models import UiAction
from .ui_routes import install_ui_routes, page_response
from starlette.concurrency import run_in_threadpool

if TYPE_CHECKING:
    from book_library.service import Library

LOG = logging.getLogger(__name__)
HERE = Path(__file__).parent
COOKIE = "book_library_csrf"
MAX_BODY = 32_768
MODES = ("focused", "balanced", "exploratory")
FACET_FIELDS = {"subjects", "disciplines", "themes", "motifs", "questions", "forms", "people", "places", "works", "artworks", "biblical_references", "periods", "cultures", "traditions", "media", "approaches"}
EDIT_FIELDS = sorted(FACET_FIELDS | {"title", "author", "attribution", "notes"})


def _identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", value):
        raise HTTPException(400, "That item identifier is not valid.")
    return value


def _origin(value: str) -> tuple[str, str | None, int | None]:
    try:
        parsed = urlsplit(value)
        return parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return "", None, None


def _tags(value: str) -> list[str]:
    items = list(dict.fromkeys(item.strip() for item in re.split(r"[,\n]", value) if item.strip()))
    if len(items) > 100:
        raise HTTPException(400, "Please use 100 or fewer items in each list.")
    return items


def _reading_tables(reading: dict[str, Any]) -> dict[str, Any]:
    """Render extracted cells as safe HTML, never the book's original markup."""
    for block in reading.get("blocks", []):
        cells = block.get("cells") or block.get("attrs", {}).get("cells", [])
        if block.get("kind") != "table" or not cells:
            continue
        rows: dict[int, list] = {}
        for cell in cells[:1000]:
            if not isinstance(cell, dict):
                continue
            try:
                row = max(0, min(int(cell.get("row", 0)), 1000))
                colspan = max(1, min(int(cell.get("colspan", 1)), 100))
                rowspan = max(1, min(int(cell.get("rowspan", 1)), 100))
            except (ValueError, TypeError):
                continue
            rows.setdefault(row, []).append({"text": cell.get("text", ""), "colspan": colspan, "rowspan": rowspan, "is_header": bool(cell.get("is_header"))})
        block["table_rows"] = [rows[row] for row in sorted(rows)]
        block["table_truncated"] = len(cells) > 1000
    return reading


def create_app(library: Library | None = None, *, enable_intake_worker: bool | None = None,
               frontend_dir: Path | None = None) -> FastAPI:
    if enable_intake_worker is None:
        enable_intake_worker = library is None
    if library is None:
        from book_library.service import Library
        library = Library()
    lifespan = None
    if enable_intake_worker:
        from .intake_worker import worker_lifespan
        lifespan = lambda app: worker_lifespan(library)
    app = FastAPI(title="The Reading Room", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.library = library
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    async def call(method: str, *args: Any, **kwargs: Any) -> Any:
        return await run_in_threadpool(getattr(library, method), *args, **kwargs)

    def page(request: Request, template: str, *, status_code: int = 200, **context: Any) -> JSONResponse:
        return page_response(template, context, status_code)

    def error_response(request: Request, status_code: int, message: str, title: str = "We couldn’t open that") -> Response:
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": message}, status_code=status_code)
        return HTMLResponse('<!doctype html><html lang="en"><meta charset="utf-8"><title>' + escape(title) +
                            '</title><main><h1>' + escape(title) + '</h1><p>' + escape(message) +
                            '</p><a href="/">Return to the Reading Room</a></main></html>', status_code=status_code)

    def secure_response(response: Response) -> Response:
        nonce = response.headers.get("X-Reading-Room-Nonce", "")
        if nonce:
            del response.headers["X-Reading-Room-Nonce"]
        nonce_source = f" 'nonce-{nonce}'" if nonce else ""
        response.headers["Content-Security-Policy"] = ("default-src 'none'; style-src 'self'" + nonce_source +
            "; script-src 'self'" + nonce_source + "; connect-src 'self'; img-src 'self'; font-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.middleware("http")
    async def local_boundary(request: Request, call_next: Any) -> Any:
        if request.url.hostname not in {"localhost", "127.0.0.1", "::1"}:
            return secure_response(error_response(request, 400, "This reading room accepts localhost requests only."))
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            source = request.headers.get("origin") or request.headers.get("referer", "")
            if _origin(source) != _origin(str(request.url)):
                return secure_response(error_response(request, 403, "Open the reading room on this computer and submit the form again.", "Request not accepted"))
            try:
                length = int(request.headers.get("content-length", "0"))
                if length < 0 or length > MAX_BODY:
                    raise ValueError
            except ValueError:
                return secure_response(error_response(request, 413, "Please shorten the submitted information and try again.", "Form too large"))
        response = await call_next(request)
        return secure_response(response)

    async def form(request: Request) -> dict[str, str]:
        content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if content_type not in {"application/x-www-form-urlencoded", "application/json"}:
            raise HTTPException(415, "Submit this information using the Reading Room controls.")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_BODY:
                raise HTTPException(413, "Please shorten the submitted information.")
        try:
            if content_type == "application/json":
                action = UiAction.model_validate_json(bytes(body))
                data = {key: ("\n".join(value) if key == "books" else value[-1] if value else "")
                        if isinstance(value, list) else value for key, value in action.fields.items()}
                token = request.headers.get("x-csrf-token", "")
            else:
                values = parse_qs(body.decode("utf-8"), keep_blank_values=True, max_num_fields=200)
                data = {key: "\n".join(items) if key == "books" else items[-1] for key, items in values.items()}
                token = data.pop("csrf", "")
        except (ValueError, UnicodeDecodeError, ValidationError):
            raise HTTPException(400, "The submitted form could not be read.") from None
        cookie = request.cookies.get(COOKIE, "")
        if not re.fullmatch(r"[a-f0-9]{64}", cookie) or not hmac.compare_digest(token, cookie):
            raise HTTPException(403, "This form has expired. Open the page again and retry.")
        return data

    def check_text(value: str, maximum: int = 4000) -> str:
        if len(value) > maximum:
            raise HTTPException(400, f"Please keep this field to {maximum:,} characters or fewer.")
        return value.strip()

    @app.exception_handler(StarletteHTTPException)
    @app.exception_handler(HTTPException)
    async def known_error(request: Request, exc: HTTPException) -> Any:
        return secure_response(error_response(request, exc.status_code, str(exc.detail)))

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> Any:
        return await known_error(request, HTTPException(400, "Please check the search or form values and try again."))

    @app.exception_handler(LookupError)
    async def missing_error(request: Request, exc: LookupError) -> Any:
        return await known_error(request, HTTPException(404, "That item is not in the current library. It may have moved or been removed."))

    @app.exception_handler(ValueError)
    async def value_error(request: Request, exc: ValueError) -> Any:
        return await known_error(request, HTTPException(400, "The library could not accept those values. Please check them and try again."))

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception) -> Any:
        LOG.exception("Reading room request failed", exc_info=exc)
        return await known_error(request, HTTPException(500, "The library could not finish this request. Your source books have not been changed. Check Library status, then try again."))

    async def search_result(q: str, focus: str, mode: str, subject: str, form_name: str, book: str, author: str, limit: int = 12, connections: int = 5, engine: str | None = None, text_type: str = "") -> dict[str, Any]:
        if mode not in ("", *MODES):
            raise HTTPException(400, "Choose focused, balanced, or exploratory search.")
        if focus:
            _identifier(focus)
        filters = {key: value for key, value in {"subject": subject, "form": form_name, "book": book, "author": author, "text_type": text_type}.items() if value}
        options = {"focus": focus or None, "mode": mode or None, "filters": filters or None, "limit": limit, "connections": connections}
        if engine:
            options["engine"] = engine
        return await call("search", q, **options)

    @app.get("/", response_class=HTMLResponse)
    @app.get("/search", response_class=HTMLResponse)
    async def search_page(request: Request, q: str = Query("", max_length=1500), focus: str = Query("", max_length=160), mode: str = "", subject: str = Query("", max_length=200), form: str = Query("", max_length=200), book: str = Query("", max_length=200), author: str = Query("", max_length=200), text_type: str = Query("", max_length=40)) -> HTMLResponse:
        q = q.strip()
        if mode not in ("", *MODES):
            raise HTTPException(400, "Choose focused, balanced, or exploratory search.")
        if focus:
            _identifier(focus)
        # A browser URL restores the form, never a search execution. This also
        # protects a restored page whose tab-session result snapshot is missing.
        # The longstanding /api/search GET contract remains exact-only below;
        # the React interface executes both exact and combined searches by POST.
        return page(request, "search.html", title="Find a thread", nav="search", q=q, selected_focus=focus, mode=mode, filters={"subject": subject, "form": form, "book": book, "author": author, "text_type": text_type}, focuses=await call("focuses"), result=None, engine="",
                    message="Your search is ready. Choose Search to see results." if q else "")

    @app.post('/search', response_class=HTMLResponse)
    async def combined_search(request:Request):
        data=await form(request)
        q=check_text(data.get('q',''),1500)
        if not q:raise HTTPException(400,'Enter a research question.')
        selected=data.get('engine','')
        if selected not in ('','exact'):raise HTTPException(400,'Choose combined or exact search.')
        focus=check_text(data.get('focus',''),160);mode=data.get('mode','')
        filters={key:check_text(data.get(key,''),200) for key in ('subject','form','book','author','text_type')}
        result=await search_result(q,focus,mode,filters['subject'],filters['form'],filters['book'],filters['author'],engine=selected or None,text_type=filters['text_type'])
        return page(request,'search.html',title='Find a thread',nav='search',q=q,selected_focus=focus,mode=mode,filters=filters,focuses=await call('focuses'),result=result,engine=selected)

    @app.get("/taxonomy", response_class=HTMLResponse)
    async def taxonomy_page(request: Request, q: str = Query("", max_length=300), offset: int = Query(0, ge=0, le=100000)) -> HTMLResponse:
        from .taxonomy import list_taxonomy
        vocabulary = await run_in_threadpool(list_taxonomy, library, query=q, offset=offset, limit=100)
        next_url = "/taxonomy?" + urlencode({"q": q, "offset": offset + 100}) if vocabulary["has_more"] else None
        previous_url = "/taxonomy?" + urlencode({"q": q, "offset": max(0, offset - 100)}) if offset else None
        return page(request, "taxonomy.html", title="Subjects and connections", nav="taxonomy", vocabulary=vocabulary, next_url=next_url, previous_url=previous_url)

    @app.get("/taxonomy/browse", response_class=HTMLResponse)
    async def taxonomy_books_page(request: Request, facet: str = Query(..., max_length=50), value: str = Query(..., max_length=200), offset: int = Query(0, ge=0, le=100000), sort: str = Query("title", pattern="^(title|creator)$")) -> HTMLResponse:
        from .taxonomy import facet_books, list_taxonomy
        result = await run_in_threadpool(facet_books, library, facet, value, offset=offset, limit=50, sort=sort)
        vocabulary = await run_in_threadpool(list_taxonomy, library, query=value, limit=100)
        label = next((f["label"] for f in vocabulary["facets"] if f["facet"] == facet and f["value"] == value), value)
        next_url = "/taxonomy/browse?" + urlencode({"facet": facet, "value": value, "sort": sort, "offset": offset + 50}) if result["has_more"] else None
        previous_url = "/taxonomy/browse?" + urlencode({"facet": facet, "value": value, "sort": sort, "offset": max(0, offset - 50)}) if offset else None
        return page(request, "taxonomy_books.html", title=label, nav="taxonomy", result=result, label=label, sort=sort, first_page_url="/taxonomy/browse?" + urlencode({"facet": facet, "value": value, "sort": sort}), next_url=next_url, previous_url=previous_url)

    async def vocabulary_editor(request: Request, term_id: str | None, *, label: str = "", saved: bool = False, error: str | None = None, submitted: dict | None = None) -> HTMLResponse:
        from .taxonomy import _vocabulary, get_term
        if term_id is not None:
            term = await run_in_threadpool(get_term, library, term_id)
            if term is None: raise HTTPException(404, "That vocabulary term is not available.")
        else:
            term = {"label": label, "aliases": [], "broader": [], "narrower": [], "related": [], "evidence": ""}
        vocabulary = await run_in_threadpool(_vocabulary, library)
        labels = {t["id"]: t.get("label", t["id"]) for t in vocabulary["terms"]}
        if submitted is not None: term = submitted
        relation_labels = {field: [labels.get(value, value) for value in term.get(field, [])] for field in ("broader", "narrower", "related")}
        return page(request, "taxonomy_term.html", status_code=400 if error else 200, title="Add a vocabulary term" if term_id is None else "Edit a vocabulary term", nav="taxonomy", term=term, relation_labels=relation_labels, creating=term_id is None, saved=saved, error=error, action="/taxonomy/new" if term_id is None else "/taxonomy/term/" + quote(term_id, safe=""))

    @app.get("/taxonomy/new", response_class=HTMLResponse)
    async def new_taxonomy_term(request: Request, label: str = Query("", max_length=160)) -> HTMLResponse:
        return await vocabulary_editor(request, None, label=label)

    @app.get("/taxonomy/term/{term_id}", response_class=HTMLResponse)
    async def edit_taxonomy_term(request: Request, term_id: str, saved: bool = False) -> HTMLResponse:
        return await vocabulary_editor(request, _identifier(term_id), saved=saved)

    async def save_vocabulary_form(request: Request, existing_id: str | None) -> HTMLResponse:
        from .taxonomy import save_term, term_id
        data = await form(request)
        submitted = {"label": check_text(data.get("label", ""), 160), "evidence": check_text(data.get("evidence", ""), 4000), "origin": "user", "status": "confirmed"}
        for field in ("aliases", "broader", "narrower", "related"):
            submitted[field] = _tags(data.get(field, ""))
        identifier = existing_id or term_id(submitted["label"])
        if existing_id is None: submitted["create_only"] = True
        try:
            # Creating a term must never silently overwrite an existing entry.
            if existing_id is None:
                from .taxonomy import get_term
                if await run_in_threadpool(get_term, library, identifier):
                    raise ValueError("That vocabulary term already exists. Open its entry to edit it.")
            await run_in_threadpool(save_term, library, identifier, submitted)
        except ValueError as exc:
            return await vocabulary_editor(request, existing_id, error=str(exc), submitted=submitted)
        return RedirectResponse("/taxonomy/term/" + quote(identifier, safe="") + "?saved=true", status_code=303)

    @app.post("/taxonomy/new", response_class=HTMLResponse)
    async def create_taxonomy_term(request: Request) -> HTMLResponse:
        return await save_vocabulary_form(request, None)

    @app.post("/taxonomy/term/{term_id}", response_class=HTMLResponse)
    async def update_taxonomy_term(request: Request, term_id: str) -> HTMLResponse:
        return await save_vocabulary_form(request, _identifier(term_id))

    @app.get("/books", response_class=HTMLResponse)
    async def books_page(request: Request, q: str = Query("", max_length=300), facet: str = Query("", max_length=200), offset: int = Query(0, ge=0, le=100000), sort: str = Query("title", pattern="^(title|creator)$")) -> HTMLResponse:
        result = await call("browse_books", query=q, facet=facet or None, sort=sort, limit=50, offset=offset)
        params = {"q": q, "facet": facet, "sort": sort}
        next_url = "/books?" + urlencode({**params, "offset": offset + 50}) if result["has_more"] else None
        previous_url = "/books?" + urlencode({**params, "offset": max(0, offset - 50)}) if offset else None
        return page(request, "books.html", title="The shelves", nav="books", books=result["books"], result=result, q=q, facet=facet, sort=sort, next_url=next_url, previous_url=previous_url, clear_filter_url="/books?" + urlencode({"q": q, "sort": sort}), first_page_url="/books?" + urlencode(params))

    @app.get("/books/{work_id}", response_class=HTMLResponse)
    async def book_page(request: Request, work_id: str, section_offset: int = Query(0, ge=0, le=100000), outline_offset: int = Query(0, ge=0, le=100000)) -> HTMLResponse:
        work = await call("book", _identifier(work_id), section_offset=section_offset, section_limit=100)
        if not work:
            raise HTTPException(404, "That book is not in the current library.")
        outline = await call("contents", work_id, offset=outline_offset, limit=100) if hasattr(library, "contents") else None
        return page(request, "book.html", title=work.get("title", "Book"), nav="books", work=work, section_offset=section_offset, outline=outline)

    @app.get("/read/{passage_id}", response_class=HTMLResponse)
    async def read_page(request: Request, passage_id: str, context: int = Query(3, ge=0, le=8), offset: int = Query(0, ge=0), saved: bool = False) -> HTMLResponse:
        reading = await call("read", _identifier(passage_id), context=context, **({"offset":offset} if offset else {}))
        if not reading:
            raise HTTPException(404, "That passage is not in the current library.")
        return page(request, "read.html", title=reading["passage"].get("title", "Read a passage"), nav="books", reading=_reading_tables(reading), context=context, saved=saved)

    @app.get("/illustration/{file_id}", response_class=HTMLResponse)
    async def illustration_page(request: Request, file_id: str, member: str = Query(..., max_length=2048)) -> HTMLResponse:
        from .media import MediaUnavailable, describe_image
        try:
            illustration = await run_in_threadpool(describe_image, library, _identifier(file_id), member)
        except MediaUnavailable as exc:
            raise HTTPException(404, str(exc)) from None
        return page(request, "illustration.html", title="Source illustration", nav="books", illustration=illustration)

    @app.get("/image/{file_id}")
    async def source_image(file_id: str, member: str = Query(..., max_length=2048)) -> Response:
        from .media import MediaUnavailable, load_image
        try:
            data, media_type, description = await run_in_threadpool(load_image, library, _identifier(file_id), member)
        except MediaUnavailable as exc:
            raise HTTPException(404, str(exc)) from None
        return Response(data, media_type=media_type, headers={"Cross-Origin-Resource-Policy": "same-origin", "Content-Disposition": "inline"})

    @app.get("/covers/{work_id}")
    async def work_cover(work_id: str) -> Response:
        from .covers import load_cover
        from .media import MediaUnavailable
        try:
            data, media_type = await run_in_threadpool(load_cover, library, _identifier(work_id))
        except MediaUnavailable as exc:
            raise HTTPException(404, str(exc)) from None
        return Response(data, media_type=media_type, headers={"Cross-Origin-Resource-Policy": "same-origin", "Content-Disposition": "inline"})

    @app.get("/focus", response_class=HTMLResponse)
    async def focuses_page(request: Request) -> HTMLResponse:
        return page(request, "focuses.html", title="Research focuses", nav="focus", focuses=await call("focuses"))

    @app.get("/focus/{focus_id}", response_class=HTMLResponse)
    async def focus_page(request: Request, focus_id: str, saved: bool = False) -> HTMLResponse:
        _identifier(focus_id)
        item = {} if focus_id == "new" else next((item for item in await call("focuses") if item.get("id") == focus_id), None)
        if item is None:
            raise HTTPException(404, "That research focus is not available.")
        return page(request, "focus.html", title="New research focus" if focus_id == "new" else "Edit research focus", nav="focus", focus=item, focus_id=focus_id, saved=saved)

    async def existing_focus(focus_id: str) -> dict[str, Any]:
        _identifier(focus_id)
        item = next((item for item in await call("focuses") if item.get("id") == focus_id), None)
        if item is None:
            raise HTTPException(404, "That research focus is not available.")
        return item

    @app.get("/focus/{focus_id}/books", response_class=HTMLResponse)
    async def focus_books_page(request: Request, focus_id: str, q: str = Query("", max_length=300), offset: int = Query(0, ge=0, le=100000), saved: bool = False, sort: str = Query("title", pattern="^(title|creator)$")) -> HTMLResponse:
        focus = await existing_focus(focus_id)
        result = await call("browse_books", query=q, sort=sort, limit=50, offset=offset)
        base = f"/focus/{quote(focus_id, safe='')}/books?"
        params = {"q": q, "sort": sort}
        next_url = base + urlencode({**params, "offset": offset + 50}) if result["has_more"] else None
        previous_url = base + urlencode({**params, "offset": max(0, offset - 50)}) if offset else None
        return page(request, "focus-books.html", title="Preferred books", nav="focus", focus=focus, books=result["books"], result=result, q=q, sort=sort, offset=offset, saved=saved, next_url=next_url, previous_url=previous_url, first_page_url=base + urlencode(params))

    @app.post("/focus/{focus_id}/books")
    async def save_focus_books(request: Request, focus_id: str) -> RedirectResponse:
        data = await form(request)
        focus = await existing_focus(focus_id)
        q = check_text(data.get("q", ""), 300)
        try:
            offset = int(data.get("offset", "0"))
        except ValueError:
            raise HTTPException(400, "Choose a valid page of books.") from None
        if not 0 <= offset <= 100000:
            raise HTTPException(400, "Choose a valid page of books.")
        sort = data.get("sort", "title")
        if sort not in ("title", "creator"):
            raise HTTPException(400, "Choose title or creator ordering.")
        result = await call("browse_books", query=q, sort=sort, limit=50, offset=offset)
        visible = {item["id"] for item in result["books"]}
        selected = _tags(check_text(data.get("books", ""), 12000))
        if not set(selected).issubset(visible):
            raise HTTPException(400, "The book list has changed. Open this page again before saving.")
        preserved = [book_id for book_id in focus.get("books", []) if book_id not in visible]
        await call("save_focus", focus_id, {**focus, "books": list(dict.fromkeys(preserved + selected))})
        return RedirectResponse(f"/focus/{quote(focus_id, safe='')}/books?" + urlencode({"q": q, "sort": sort, "offset": offset, "saved": "true"}), status_code=303)

    @app.post("/focus/{focus_id}")
    async def save_focus(request: Request, focus_id: str) -> RedirectResponse:
        data = await form(request)
        _identifier(focus_id)
        name = check_text(data.get("name", ""), 120)
        if not name:
            raise HTTPException(400, "Give this research focus a name.")
        breadth = data.get("breadth", "balanced")
        if breadth not in MODES:
            raise HTTPException(400, "Choose a valid breadth for this focus.")
        if focus_id == "new":
            focus_id = "focus-" + secrets.token_hex(6)
        existing = next((item for item in await call("focuses") if item.get("id") == focus_id), {})
        await call("save_focus", focus_id, {**existing, "name": name, "description": check_text(data.get("description", "")), "subjects": _tags(check_text(data.get("subjects", ""))), "forms": _tags(check_text(data.get("forms", ""))), "books": existing.get("books", []), "breadth": breadth})
        return RedirectResponse(f"/focus/{quote(focus_id, safe='')}?saved=true", status_code=303)

    async def metadata_target(target_id: str, work_id: str, section_offset: int = 0) -> tuple[dict, dict]:
        work = await call("book", _identifier(work_id), section_offset=section_offset, section_limit=100)
        _identifier(target_id)
        if not work:
            raise HTTPException(404, "That book is not available.")
        if target_id == work.get("id"):
            return work, work
        section = (await call("section", target_id, work_id=work_id)) if hasattr(library, "section") else next((item for item in work.get("sections", []) if item.get("id") == target_id), None)
        if section is None:
            raise HTTPException(404, "That section is not part of this book.")
        if section.get("passage_id"):
            reading = await call("read", section["passage_id"], context=0)
            section = {**section, **reading.get("section", {})}
        return work, section

    @app.get("/metadata/{target_id}", response_class=HTMLResponse)
    async def metadata_page(request: Request, target_id: str, work: str = Query(..., max_length=160), section_offset: int = Query(0, ge=0, le=100000), saved: bool = False) -> HTMLResponse:
        item, target = await metadata_target(target_id, work, section_offset)
        fields = EDIT_FIELDS if target_id == item["id"] else sorted(FACET_FIELDS | {"title", "attribution", "text_type"})
        from .passage_segments import TEXT_TYPES
        kinds = {field: field for field in FACET_FIELDS} | {"author": "authors", "attribution": "authors"}
        return page(request, "metadata.html", title="Correct library information", nav="books", work=item, target=target, target_id=target_id, fields=fields, saved=saved, section_offset=section_offset, suggestion_kinds=kinds, text_types=sorted(TEXT_TYPES))

    @app.post("/metadata/{target_id}")
    async def annotate(request: Request, target_id: str) -> RedirectResponse:
        data = await form(request)
        work_id = data.get("work_id", "")
        try:
            section_offset = int(data.get("section_offset", "0"))
        except ValueError:
            raise HTTPException(400, "Choose a valid contents page.") from None
        if not 0 <= section_offset <= 100000:
            raise HTTPException(400, "Choose a valid contents page.")
        work, target = await metadata_target(target_id, work_id, section_offset)
        field = data.get("field", "")
        allowed = EDIT_FIELDS if target_id == work["id"] else FACET_FIELDS | {"title", "attribution", "text_type"}
        if field not in allowed:
            raise HTTPException(400, "Choose a supported field to correct.")
        raw = check_text(data.get("value", ""), 6000)
        evidence = check_text(data.get("evidence", ""), 4000)
        if not raw or not evidence:
            raise HTTPException(400, "Supply both a correction and the source or reason for it.")
        if data.get("value_format") == "json":
            try:
                value = json.loads(raw)
            except (ValueError, RecursionError):
                raise HTTPException(400, "The structured value is not valid JSON.") from None
        else:
            value = _tags(raw) if field in FACET_FIELDS else raw
        if field in FACET_FIELDS:
            if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                raise HTTPException(400, "A subject or other facet must contain a list of text values.")
            value = {field: value}
            field = "profile"
        await call("annotate", target_id, {"field": field, "value": value, "evidence": evidence, "status": "confirmed", "origin": "user"})
        await call("sync")
        return RedirectResponse("/metadata/" + quote(target_id, safe="") + "?" + urlencode({"work": work_id, "section_offset": section_offset, "saved": "true"}), status_code=303)

    @app.get("/history", response_class=HTMLResponse)
    async def history_page(request: Request, limit: int = Query(50, ge=1, le=100)) -> HTMLResponse:
        return page(request, "history.html", title="Research history", nav="history", entries=await call("usage", limit=limit))

    @app.post("/usage")
    async def record_usage(request: Request) -> RedirectResponse:
        data = await form(request)
        passage_id = _identifier(data.get("passage_id", ""))
        reading = await call("read", passage_id, context=0)
        if not reading:
            raise HTTPException(404, "That passage is no longer available.")
        purpose = data.get("purpose", "research")
        if purpose not in {"research", "class-prep", "substack", "efm"}:
            raise HTTPException(400, "Choose a valid research purpose.")
        kind = data.get("kind", "inspected")
        if kind not in {"inspected", "offered", "recommended", "selected", "used"}:
            raise HTTPException(400, "Choose a valid research activity.")
        passage = reading["passage"]
        notes = check_text(data.get("notes", ""))
        await call("record_usage", {"passage_id": passage_id, "work_id": passage.get("work_id"), "title": passage.get("title"), "purpose": purpose, "kind": kind, "location": passage.get("location"), "attribution": passage.get("attribution"), "notes": notes, "evidence": notes or f"User explicitly recorded this passage as {kind} in the Reading Room.", "destination": check_text(data.get("destination", ""), 1000)})
        return RedirectResponse(f"/read/{quote(passage_id, safe='')}?saved=true#research-record", status_code=303)

    @app.get("/status", response_class=HTMLResponse)
    async def status_page(request: Request, synced: bool = False) -> HTMLResponse:
        return page(request, "status.html", title="Library status", nav="status", status=await call("status"), synced=synced)

    @app.post("/sync")
    async def sync(request: Request) -> RedirectResponse:
        await form(request)
        await call("sync")
        return RedirectResponse("/status?synced=true", status_code=303)

    @app.get("/api/status")
    async def api_status() -> Any:
        return await call("status")

    @app.get("/api/form-options")
    async def api_form_options(kind: str = Query(..., max_length=50), q: str = Query("", max_length=300), limit: int = Query(30, ge=1, le=100), exclude: str = Query("", max_length=200)) -> Any:
        from .form_options import suggestions
        return await run_in_threadpool(suggestions, library, kind, query=q, limit=limit, exclude=exclude)

    @app.get("/api/search")
    async def api_search(q: str = Query(..., min_length=1, max_length=1500), focus: str = Query("", max_length=160), mode: str = "", subject: str = Query("", max_length=200), form: str = Query("", max_length=200), book: str = Query("", max_length=200), author: str = Query("", max_length=200), limit: int = Query(12, ge=1, le=50), connections: int = Query(5, ge=0, le=20), text_type: str = Query("", max_length=40)) -> Any:
        return await search_result(q, focus, mode, subject, form, book, author, limit, connections,engine='exact',text_type=text_type)

    @app.get("/api/read/{passage_id}")
    async def api_read(passage_id: str, context: int = Query(3, ge=0, le=8), offset: int = Query(0, ge=0)) -> Any:
        return await call("read", _identifier(passage_id), context=context, **({"offset":offset} if offset else {}))

    @app.get("/api/books")
    async def api_books(q: str = Query("", max_length=300), facet: str = Query("", max_length=200), limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0, le=100000)) -> Any:
        return await call("books", query=q, facet=facet or None, limit=limit, offset=offset)

    @app.get("/api/books/{work_id}")
    async def api_book(work_id: str, section_offset: int = Query(0, ge=0, le=100000), section_limit: int = Query(100, ge=1, le=100)) -> Any:
        return await call("book", _identifier(work_id), section_offset=section_offset, section_limit=section_limit)

    @app.get("/api/focus")
    async def api_focus() -> Any:
        return await call("focuses")

    @app.get("/api/history")
    async def api_history(limit: int = Query(50, ge=1, le=100)) -> Any:
        return await call("usage", limit=limit)

    from .unit_web import register_unit_routes
    register_unit_routes(app, library, page, form, call, _identifier, _reading_tables)
    from .citation_web import register_citation_routes
    register_citation_routes(app,library,page,form,call,_identifier)
    from .review_web import register_review_routes
    register_review_routes(app, library, page, form, call, _identifier)
    from .intake_web import register_intake_routes
    register_intake_routes(app, library, page, form, call, _identifier)
    install_ui_routes(app, frontend_dir or HERE / "frontend_dist")
    return app
