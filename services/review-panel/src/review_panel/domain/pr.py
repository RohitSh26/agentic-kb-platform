"""The pull-request snapshot the panel reviews. All text fields are UNTRUSTED."""

from pydantic import BaseModel, ConfigDict


class PRContext(BaseModel):
    model_config = ConfigDict(frozen=True)

    repo: str  # "owner/name"
    number: int
    head_sha: str
    title: str
    body: str
    author: str
    diff: str
