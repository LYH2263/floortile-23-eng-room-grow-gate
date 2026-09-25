from pydantic import BaseModel


class RoomUpdate(BaseModel):
    length: float | None = None
    width: float | None = None
