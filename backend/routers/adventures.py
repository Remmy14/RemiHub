"""Authenticated Adventures V1 API. The owner is always derived from Firebase auth."""
from __future__ import annotations

import calendar as calendar_lib
from datetime import date
from tempfile import SpooledTemporaryFile
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.routing import APIRoute
from fastapi.responses import FileResponse
from psycopg2 import IntegrityError

from backend.core.auth import AuthenticatedPrincipal, require_current_principal
from backend.models import adventures_models as m
from backend.services import adventures_media as media, adventures_service as service

class BoundedPhotoUploadRoute(APIRoute):
    """Cap multipart bytes while Starlette is ingesting them, before spooling."""

    def get_route_handler(self):
        handler = super().get_route_handler()
        if self.path != "/adventures/trips/{trip_id}/photos" or "POST" not in self.methods:
            return handler

        async def bounded(request: Request):
            try:
                # Allows multipart framing and the caption, but remains bounded
                # even for chunked requests or dishonest Content-Length headers.
                limit = media.max_bytes() + 65536
            except service.ConfigurationError as exc:
                raise _error(exc) from exc
            claimed = request.headers.get("content-length")
            if claimed is not None:
                try:
                    declared_size = int(claimed)
                except ValueError as exc:
                    raise HTTPException(400, "Invalid Content-Length") from exc
                if declared_size > limit:
                    raise HTTPException(413, "Photo multipart request exceeds configured size limit")
            # Stage at most the allowed body before the multipart parser opens
            # any UploadFile. This also closes the bounded staging file on 413.
            with SpooledTemporaryFile(max_size=1024 * 1024) as staged:
                received = 0
                async for chunk in request.stream():
                    received += len(chunk)
                    if received > limit:
                        raise HTTPException(413, "Photo multipart request exceeds configured size limit")
                    staged.write(chunk)
                staged.seek(0)

                async def replay_receive():
                    data = staged.read(65536)
                    return {"type": "http.request", "body": data, "more_body": bool(data)}

                return await handler(Request(request.scope, receive=replay_receive))

        return bounded


router = APIRouter(
    prefix="/adventures", tags=["Adventures"],
    route_class=BoundedPhotoUploadRoute,
    responses={401: {"description": "Missing or invalid Firebase bearer token"},
               403: {"description": "Inactive or unapproved RemiHub user"},
               404: {"description": "Resource absent or not owned by caller"},
               409: {"description": "Referenced campground, checklist precondition, or calendar cap"},
               503: {"description": "Required Adventures configuration unavailable"}},
)
DEFAULT_CATEGORIES = ["Before Departure", "At Campground", "Before Heading Home"]


def _error(exc: ValueError):
    code = (404 if isinstance(exc, service.NotFound) else 409 if isinstance(exc, service.Conflict)
            else 503 if isinstance(exc, service.ConfigurationError) else 400)
    return HTTPException(status_code=code, detail=str(exc))


def _call(fn, *args, **kwargs):
    try:
        return {"success": True, "data": fn(*args, **kwargs)}
    except ValueError as exc:
        raise _error(exc) from exc
    except IntegrityError as exc:
        raise HTTPException(status_code=409, detail="Adventures resource changed concurrently") from exc


def _body(model, patch=False):
    return model.model_dump(mode="json", exclude_unset=patch)


def _identity(principal):
    return principal.id


@router.get("/dashboard", response_model=m.Envelope[m.Dashboard])
def dashboard(year: int = Query(..., ge=1, le=9998), upcoming_limit: int = Query(20, ge=1, le=100),
              principal: AuthenticatedPrincipal = Depends(require_current_principal)):
    return _call(service.dashboard, _identity(principal), year, upcoming_limit)


@router.get("/calendar", response_model=m.Envelope[list[m.CalendarEntry]])
def calendar(month: str | None = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
             start_date: date | None = None, end_date: date | None = None,
             principal: AuthenticatedPrincipal = Depends(require_current_principal)):
    if month:
        if start_date or end_date:
            raise HTTPException(400, "Use month or start_date/end_date")
        year, number = (int(x) for x in month.split("-"))
        if year < 1:
            raise HTTPException(400, "Invalid calendar year")
        start_date = date(year, number, 1)
        end_date = date(year, number, calendar_lib.monthrange(year, number)[1])
    elif start_date is None or end_date is None:
        raise HTTPException(400, "Supply month or both start_date and end_date")
    return _call(service.calendar, _identity(principal), start_date, end_date)


@router.get("/campgrounds", response_model=m.Envelope[m.Page[m.CampgroundOut]])
def list_campgrounds(limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0),
                     principal: AuthenticatedPrincipal = Depends(require_current_principal)):
    return _call(service.list_campgrounds, _identity(principal), limit, offset)


@router.post("/campgrounds", status_code=201, response_model=m.Envelope[m.CampgroundOut])
def create_campground(request: m.CampgroundInput, principal: AuthenticatedPrincipal = Depends(require_current_principal)):
    return _call(service.create_campground, _identity(principal), _body(request))


@router.get("/campgrounds/{campground_id}", response_model=m.Envelope[m.CampgroundOut])
def get_campground(campground_id: UUID, principal: AuthenticatedPrincipal = Depends(require_current_principal)):
    return _call(service.get_campground, _identity(principal), str(campground_id))


@router.patch("/campgrounds/{campground_id}", response_model=m.Envelope[m.CampgroundOut])
def update_campground(campground_id: UUID, request: m.CampgroundPatch,
                      principal: AuthenticatedPrincipal = Depends(require_current_principal)):
    return _call(service.update_campground, _identity(principal), str(campground_id), _body(request, True))


@router.delete("/campgrounds/{campground_id}", status_code=204)
def delete_campground(campground_id: UUID, principal: AuthenticatedPrincipal = Depends(require_current_principal)):
    _call(service.delete_campground, _identity(principal), str(campground_id))


@router.get("/campgrounds/{campground_id}/visits", response_model=m.Envelope[m.CampgroundVisits])
def campground_visits(campground_id: UUID, limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0),
                      principal: AuthenticatedPrincipal = Depends(require_current_principal)):
    return _call(service.campground_visits, _identity(principal), str(campground_id), limit, offset)


@router.get("/trips", response_model=m.Envelope[m.Page[m.TripOut]])
def list_trips(kind: Literal["all", "upcoming", "active", "historical", "cancelled", "undated"] = "all",
               year: int | None = Query(None, ge=1, le=9998), limit: int = Query(50, ge=1, le=100),
               offset: int = Query(0, ge=0), principal: AuthenticatedPrincipal = Depends(require_current_principal)):
    return _call(service.list_trips, _identity(principal), kind, year, limit, offset)


@router.post("/trips", status_code=201, response_model=m.Envelope[m.TripOut])
def create_trip(request: m.TripInput, principal: AuthenticatedPrincipal = Depends(require_current_principal)):
    return _call(service.create_trip, _identity(principal), _body(request))


@router.get("/trips/{trip_id}", response_model=m.Envelope[m.TripOut])
def get_trip(trip_id: UUID, principal: AuthenticatedPrincipal = Depends(require_current_principal)):
    return _call(service.get_trip, _identity(principal), str(trip_id))


@router.patch("/trips/{trip_id}", response_model=m.Envelope[m.TripOut])
def update_trip(trip_id: UUID, request: m.TripPatch,
                principal: AuthenticatedPrincipal = Depends(require_current_principal)):
    return _call(service.update_trip, _identity(principal), str(trip_id), _body(request, True))


@router.delete("/trips/{trip_id}", status_code=204)
def delete_trip(trip_id: UUID, principal: AuthenticatedPrincipal = Depends(require_current_principal)):
    _call(service.delete_trip, _identity(principal), str(trip_id))
    try:
        media.reconcile_cleanup()
    except service.ConfigurationError:
        # The committed cleanup queue is retained; storage remains an operator blocker.
        pass


@router.patch("/trips/{trip_id}/notes", response_model=m.Envelope[m.TripOut])
def update_notes(trip_id: UUID, request: m.NotesPatch,
                 principal: AuthenticatedPrincipal = Depends(require_current_principal)):
    return _call(service.update_trip, _identity(principal), str(trip_id), _body(request, True))


@router.get("/trips/{trip_id}/activities", response_model=m.Envelope[m.Page[m.ActivityOut]])
def list_activities(trip_id: UUID, limit: int = Query(100, ge=1, le=200), offset: int = Query(0, ge=0),
                    principal: AuthenticatedPrincipal = Depends(require_current_principal)):
    return _call(service.list_child, _identity(principal), str(trip_id), "adventures_activities",
                 "activity_date NULLS LAST,display_order,id", limit, offset)


@router.post("/trips/{trip_id}/activities", status_code=201, response_model=m.Envelope[m.ActivityOut])
def create_activity(trip_id: UUID, request: m.ActivityInput,
                    principal: AuthenticatedPrincipal = Depends(require_current_principal)):
    return _call(service.create_child, _identity(principal), str(trip_id), "adventures_activities", _body(request))


@router.get("/trips/{trip_id}/activities/{activity_id}", response_model=m.Envelope[m.ActivityOut])
def get_activity(trip_id: UUID, activity_id: UUID,
                 principal: AuthenticatedPrincipal = Depends(require_current_principal)):
    return _call(service.get_child, _identity(principal), str(trip_id), "adventures_activities", str(activity_id))


@router.patch("/trips/{trip_id}/activities/{activity_id}", response_model=m.Envelope[m.ActivityOut])
def update_activity(trip_id: UUID, activity_id: UUID, request: m.ActivityPatch,
                    principal: AuthenticatedPrincipal = Depends(require_current_principal)):
    return _call(service.update_child, _identity(principal), str(trip_id), "adventures_activities",
                 str(activity_id), _body(request, True))


@router.delete("/trips/{trip_id}/activities/{activity_id}", status_code=204)
def delete_activity(trip_id: UUID, activity_id: UUID,
                    principal: AuthenticatedPrincipal = Depends(require_current_principal)):
    _call(service.delete_child, _identity(principal), str(trip_id), "adventures_activities", str(activity_id))


@router.get("/checklist-template", response_model=m.Envelope[m.Template])
def template(limit: int = Query(200, ge=1, le=500), offset: int = Query(0, ge=0),
             principal: AuthenticatedPrincipal = Depends(require_current_principal)):
    return {"success": True, "data": {"categories": DEFAULT_CATEGORIES,
        "items": service.list_template(_identity(principal), limit, offset)}}


@router.post("/checklist-template/items", status_code=201, response_model=m.Envelope[m.ChecklistItemOut])
def create_template_item(request: m.ChecklistItemInput,
                         principal: AuthenticatedPrincipal = Depends(require_current_principal)):
    return _call(service.create_template_item, _identity(principal), _body(request))


@router.get("/checklist-template/items/{item_id}", response_model=m.Envelope[m.ChecklistItemOut])
def get_template_item(item_id: UUID, principal: AuthenticatedPrincipal = Depends(require_current_principal)):
    return _call(service.get_template_item, _identity(principal), str(item_id))


@router.patch("/checklist-template/items/{item_id}", response_model=m.Envelope[m.ChecklistItemOut])
def update_template_item(item_id: UUID, request: m.ChecklistItemPatch,
                         principal: AuthenticatedPrincipal = Depends(require_current_principal)):
    return _call(service.update_template_item, _identity(principal), str(item_id), _body(request, True))


@router.delete("/checklist-template/items/{item_id}", status_code=204)
def delete_template_item(item_id: UUID, principal: AuthenticatedPrincipal = Depends(require_current_principal)):
    _call(service.delete_template_item, _identity(principal), str(item_id))


@router.get("/trips/{trip_id}/checklist", response_model=m.Envelope[m.Checklist])
def get_checklist(trip_id: UUID, principal: AuthenticatedPrincipal = Depends(require_current_principal)):
    return _call(service.get_checklist, _identity(principal), str(trip_id))


@router.post("/trips/{trip_id}/checklist/instantiate", response_model=m.Envelope[m.Checklist])
def instantiate_checklist(trip_id: UUID, principal: AuthenticatedPrincipal = Depends(require_current_principal)):
    return _call(service.instantiate_checklist, _identity(principal), str(trip_id))


@router.post("/trips/{trip_id}/checklist/reset", response_model=m.Envelope[m.Checklist])
def reset_checklist(trip_id: UUID, principal: AuthenticatedPrincipal = Depends(require_current_principal)):
    return _call(service.reset_checklist, _identity(principal), str(trip_id))


@router.post("/trips/{trip_id}/checklist/items", status_code=201, response_model=m.Envelope[m.ChecklistItemOut])
def create_trip_item(trip_id: UUID, request: m.TripItemInput,
                     principal: AuthenticatedPrincipal = Depends(require_current_principal)):
    return _call(service.create_trip_item, _identity(principal), str(trip_id), _body(request))


@router.get("/trips/{trip_id}/checklist/items/{item_id}", response_model=m.Envelope[m.ChecklistItemOut])
def get_trip_item(trip_id: UUID, item_id: UUID,
                  principal: AuthenticatedPrincipal = Depends(require_current_principal)):
    return _call(service.get_child, _identity(principal), str(trip_id), "adventures_trip_items", str(item_id))


@router.patch("/trips/{trip_id}/checklist/items/{item_id}", response_model=m.Envelope[m.ChecklistItemOut])
def update_trip_item(trip_id: UUID, item_id: UUID, request: m.TripItemPatch,
                     principal: AuthenticatedPrincipal = Depends(require_current_principal)):
    return _call(service.update_child, _identity(principal), str(trip_id), "adventures_trip_items",
                 str(item_id), _body(request, True))


@router.delete("/trips/{trip_id}/checklist/items/{item_id}", status_code=204)
def delete_trip_item(trip_id: UUID, item_id: UUID,
                     principal: AuthenticatedPrincipal = Depends(require_current_principal)):
    _call(service.delete_child, _identity(principal), str(trip_id), "adventures_trip_items", str(item_id))


@router.get("/trips/{trip_id}/photos", response_model=m.Envelope[m.Page[m.PhotoOut]])
def list_photos(trip_id: UUID, limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0),
                principal: AuthenticatedPrincipal = Depends(require_current_principal)):
    return _call(media.list_photos, _identity(principal), str(trip_id), limit, offset)


@router.post("/trips/{trip_id}/photos", status_code=201, response_model=m.Envelope[m.PhotoOut],
             responses={413: {"description": "Photo multipart request exceeds configured size limit"}})
async def upload_photo(trip_id: UUID, file: UploadFile = File(...), caption: str | None = Form(None, max_length=4000),
                       principal: AuthenticatedPrincipal = Depends(require_current_principal)):
    try:
        return {"success": True, "data": await media.upload(_identity(principal), str(trip_id), file, caption)}
    except ValueError as exc:
        raise _error(exc) from exc
    except IntegrityError as exc:
        raise HTTPException(status_code=409, detail="Adventures resource changed concurrently") from exc
    finally:
        await file.close()


@router.get("/trips/{trip_id}/photos/{photo_id}", response_model=m.Envelope[m.PhotoOut])
def get_photo(trip_id: UUID, photo_id: UUID,
              principal: AuthenticatedPrincipal = Depends(require_current_principal)):
    return _call(media.get, _identity(principal), str(trip_id), str(photo_id))


@router.patch("/trips/{trip_id}/photos/{photo_id}", response_model=m.Envelope[m.PhotoOut])
def update_photo(trip_id: UUID, photo_id: UUID, request: m.PhotoPatch,
                 principal: AuthenticatedPrincipal = Depends(require_current_principal)):
    return _call(media.update, _identity(principal), str(trip_id), str(photo_id), _body(request, True))


@router.delete("/trips/{trip_id}/photos/{photo_id}", status_code=204)
def delete_photo(trip_id: UUID, photo_id: UUID,
                 principal: AuthenticatedPrincipal = Depends(require_current_principal)):
    _call(media.delete, _identity(principal), str(trip_id), str(photo_id))


def _send_photo(trip_id, photo_id, variant, principal, download=False):
    try:
        path, mime, filename = media.file_for(_identity(principal), str(trip_id), str(photo_id), variant)
    except ValueError as exc:
        raise _error(exc) from exc
    return FileResponse(path, media_type=mime, filename=filename if download else None,
                        content_disposition_type="attachment" if download else "inline",
                        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})


@router.get("/trips/{trip_id}/photos/{photo_id}/thumbnail", response_class=FileResponse,
            responses={200: {"content": {"image/jpeg": {}}}})
def photo_thumbnail(trip_id: UUID, photo_id: UUID,
                    principal: AuthenticatedPrincipal = Depends(require_current_principal)):
    return _send_photo(trip_id, photo_id, "thumbnail", principal)


@router.get("/trips/{trip_id}/photos/{photo_id}/original", response_class=FileResponse,
            responses={200: {"content": {"image/jpeg": {}, "image/png": {}, "image/webp": {}}}})
def photo_original(trip_id: UUID, photo_id: UUID,
                   principal: AuthenticatedPrincipal = Depends(require_current_principal)):
    return _send_photo(trip_id, photo_id, "original", principal)


@router.get("/trips/{trip_id}/photos/{photo_id}/download", response_class=FileResponse,
            responses={200: {"content": {"image/jpeg": {}, "image/png": {}, "image/webp": {}}}})
def photo_download(trip_id: UUID, photo_id: UUID,
                   principal: AuthenticatedPrincipal = Depends(require_current_principal)):
    return _send_photo(trip_id, photo_id, "original", principal, True)


@router.put("/trips/{trip_id}/cover/{photo_id}", response_model=m.Envelope[m.TripOut])
def set_cover(trip_id: UUID, photo_id: UUID,
              principal: AuthenticatedPrincipal = Depends(require_current_principal)):
    return _call(media.set_cover, _identity(principal), str(trip_id), str(photo_id))


@router.delete("/trips/{trip_id}/cover", response_model=m.Envelope[m.TripOut])
def clear_cover(trip_id: UUID, principal: AuthenticatedPrincipal = Depends(require_current_principal)):
    return _call(media.set_cover, _identity(principal), str(trip_id), None)


@router.get("/trips/{trip_id}/journal", response_model=m.Envelope[m.JournalOut | None])
def get_journal(trip_id: UUID, principal: AuthenticatedPrincipal = Depends(require_current_principal)):
    return _call(service.get_singleton, _identity(principal), str(trip_id), "adventures_journals")


@router.put("/trips/{trip_id}/journal", response_model=m.Envelope[m.JournalOut])
def upsert_journal(trip_id: UUID, request: m.JournalInput,
                   principal: AuthenticatedPrincipal = Depends(require_current_principal)):
    return _call(service.upsert_singleton, _identity(principal), str(trip_id), "adventures_journals", _body(request, True))


@router.get("/trips/{trip_id}/memories", response_model=m.Envelope[m.Page[m.MemoryOut]])
def list_memories(trip_id: UUID, limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0),
                  principal: AuthenticatedPrincipal = Depends(require_current_principal)):
    return _call(service.list_memories, _identity(principal), str(trip_id), limit, offset)


@router.post("/trips/{trip_id}/memories", status_code=201, response_model=m.Envelope[m.MemoryOut])
def create_memory(trip_id: UUID, request: m.MemoryInput,
                  principal: AuthenticatedPrincipal = Depends(require_current_principal)):
    return _call(service.create_memory, _identity(principal), str(trip_id), _body(request))


@router.get("/trips/{trip_id}/memories/{memory_id}", response_model=m.Envelope[m.MemoryOut])
def get_memory(trip_id: UUID, memory_id: UUID,
               principal: AuthenticatedPrincipal = Depends(require_current_principal)):
    return _call(service.get_memory, _identity(principal), str(trip_id), str(memory_id))


@router.patch("/trips/{trip_id}/memories/{memory_id}", response_model=m.Envelope[m.MemoryOut])
def update_memory(trip_id: UUID, memory_id: UUID, request: m.MemoryPatch,
                  principal: AuthenticatedPrincipal = Depends(require_current_principal)):
    return _call(service.update_memory, _identity(principal), str(trip_id), str(memory_id), _body(request, True))


@router.delete("/trips/{trip_id}/memories/{memory_id}", status_code=204)
def delete_memory(trip_id: UUID, memory_id: UUID,
                  principal: AuthenticatedPrincipal = Depends(require_current_principal)):
    _call(service.delete_child, _identity(principal), str(trip_id), "adventures_memories", str(memory_id))


@router.get("/trips/{trip_id}/rating", response_model=m.Envelope[m.RatingOut | None])
def get_rating(trip_id: UUID, principal: AuthenticatedPrincipal = Depends(require_current_principal)):
    return _call(service.get_singleton, _identity(principal), str(trip_id), "adventures_ratings")


@router.put("/trips/{trip_id}/rating", response_model=m.Envelope[m.RatingOut])
def upsert_rating(trip_id: UUID, request: m.RatingInput,
                  principal: AuthenticatedPrincipal = Depends(require_current_principal)):
    return _call(service.upsert_singleton, _identity(principal), str(trip_id), "adventures_ratings", _body(request, True))
