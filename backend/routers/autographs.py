"""Legacy Android endpoints and the authenticated Autographs V2 API."""
from uuid import UUID

from fastapi import APIRouter, File, Query, UploadFile

from backend.models import autographs as m
from backend.services import autograph_service as service

router = APIRouter(prefix="/autographs", tags=["Autographs"])


@router.post("/uploadImage")
async def upload_autograph_image(file: UploadFile = File(...)):
    try:
        return await service.upload_autograph_image(file)
    finally:
        await file.close()


@router.get("/image/{filename}")
def get_autograph_image(filename: str):
    return service.get_autograph_image(filename)


@router.post("/add", response_model=m.AutographEntry)
def add_autograph(entry: m.AutographCreate):
    return service.add_autograph(entry)


@router.get("/all", response_model=list[m.AutographEntry])
def get_all_autographs():
    return service.get_all_autographs()


@router.get("/stats", response_model=m.CollectionStats)
def collection_stats():
    return service.collection_stats()


@router.get("/helmets", response_model=list[m.Helmet])
def list_helmets():
    return service.list_helmets()


@router.post("/helmets", response_model=m.Helmet, status_code=201)
def create_helmet(value: m.HelmetCreate):
    return service.create_helmet(value)


@router.get("/helmets/{helmet_id}", response_model=m.Helmet)
def get_helmet(helmet_id: UUID):
    return service.get_helmet(helmet_id)


@router.get("/helmets/{helmet_id}/autographs", response_model=list[m.ScopedAutograph])
def list_helmet_autographs(helmet_id: UUID):
    return service.list_helmet_autographs(helmet_id)


@router.post("/helmets/{helmet_id}/autographs", response_model=m.ScopedAutograph, status_code=201)
def create_autograph(helmet_id: UUID, entry: m.ScopedAutographCreate):
    return service.create_autograph(helmet_id, entry)


@router.get("/helmets/{helmet_id}/stats", response_model=m.HelmetStats)
def helmet_stats(helmet_id: UUID):
    return service.helmet_stats(helmet_id)


@router.get("/helmets/{helmet_id}/images", response_model=list[m.HelmetImage])
def list_helmet_images(helmet_id: UUID):
    return service.list_helmet_images(helmet_id)


@router.get("/helmets/{helmet_id}/images/{view}", response_model=m.HelmetImage)
def get_helmet_image(helmet_id: UUID, view: str):
    return service.get_helmet_image(helmet_id, view)


@router.put("/helmets/{helmet_id}/images/{view}", response_model=m.HelmetImage)
async def upload_helmet_image(helmet_id: UUID, view: str, file: UploadFile = File(...)):
    try:
        return await service.upload_helmet_image(helmet_id, view, file)
    finally:
        await file.close()


@router.get("/drivers", response_model=list[m.Driver])
def list_drivers(search: str | None = Query(None, max_length=200)):
    return service.list_drivers(search)


@router.get("/drivers/{driver_id}", response_model=m.Driver)
def get_driver(driver_id: UUID):
    return service.get_driver(driver_id)


@router.patch("/drivers/{driver_id}", response_model=m.Driver)
def rename_driver(driver_id: UUID, value: m.DriverCreate):
    return service.rename_driver(driver_id, value)


@router.post("/drivers/{driver_id}/merge", response_model=m.Driver)
def merge_drivers(driver_id: UUID, value: m.DriverMerge):
    return service.merge_drivers(driver_id, value.source_driver_id)


@router.get("/drivers/{driver_id}/history", response_model=list[m.DriverHistoryItem])
def driver_history(driver_id: UUID):
    return service.driver_history(driver_id)


@router.delete("/{autograph_id}")
def delete_autograph(autograph_id: int):
    return service.delete_autograph(autograph_id)
