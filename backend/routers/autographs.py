# Python Imports

# 3rd Party Imports
from fastapi import APIRouter, File, UploadFile

# Local Imports
from backend.services import autograph_service
from backend.models.autographs import AutographCreate


# Declare our Autographs module router
router = APIRouter(prefix="/autographs", tags=["Autographs"])


@router.post("/uploadImage")
async def upload_autograph_image(file: UploadFile = File(...)):
    return await autograph_service.upload_autograph_image(file)


@router.get("/image/{filename}")
def get_autograph_image(filename: str):
    return autograph_service.get_autograph_image(filename)

@router.post("/add")
def add_autograph(entry: AutographCreate):
    return autograph_service.add_autograph(entry)


@router.get("/all")
def get_all_autographs():
    return autograph_service.get_all_autographs()

@router.delete("/{autograph_id}")
def delete_autograph(autograph_id: int):
    return autograph_service.delete_autograph(autograph_id)
