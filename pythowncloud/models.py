"""Pydantic request/response models."""
from pydantic import BaseModel


class FileInfo(BaseModel):
    name: str
    path: str
    size: int
    is_dir: bool
    modified: str
    checksum: str | None = None


class DirectoryListing(BaseModel):
    path: str
    items: list[FileInfo]
    total: int


class UploadResponse(BaseModel):
    path: str
    size: int
    checksum: str
    message: str


class MoveRequest(BaseModel):
    source: str
    destination: str
