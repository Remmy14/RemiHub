from enum import Enum

from pydantic import BaseModel


class AttendanceStatus(str, Enum):
    GOING = "going"
    ATTENDED = "attended"


class AttendanceUpdate(BaseModel):
    status: AttendanceStatus
