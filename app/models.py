from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, HttpUrl, field_validator


class ExtractionMode(str, Enum):
    AUTO = "auto"
    PRODUCTS = "products"
    ARTICLES = "articles"
    TABLE = "table"
    LINKS = "links"
    CUSTOM = "custom"


class FieldRule(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    selector: str = Field(min_length=1, max_length=500)
    value: Literal["text", "html", "href", "src", "content", "value"] = "text"
    multiple: bool = False
    required: bool = False


class ScrapeRequest(BaseModel):
    url: HttpUrl
    mode: ExtractionMode = ExtractionMode.AUTO
    prompt: str | None = Field(default=None, max_length=1000)
    item_selector: str | None = Field(default=None, max_length=500)
    fields: list[FieldRule] = Field(default_factory=list, max_length=50)
    next_page_selector: str | None = Field(default=None, max_length=500)
    max_pages: int = Field(default=1, ge=1, le=20)
    max_items: int = Field(default=200, ge=1, le=5000)
    render_js: bool = True
    wait_for_selector: str | None = Field(default=None, max_length=500)
    delay_ms: int = Field(default=800, ge=0, le=10000)
    respect_robots_txt: bool = True
    same_domain_only: bool = True

    @field_validator("fields")
    @classmethod
    def custom_mode_requires_fields(cls, value: list[FieldRule], info: Any) -> list[FieldRule]:
        mode = info.data.get("mode")
        if mode == ExtractionMode.CUSTOM and not value:
            raise ValueError("El modo custom requiere al menos un campo.")
        return value


class ScrapeResult(BaseModel):
    job_id: str
    status: Literal["completed", "failed", "running"]
    source_url: str
    pages_visited: int = 0
    item_count: int = 0
    data: list[dict[str, Any]] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    created_at: str
    finished_at: str | None = None
