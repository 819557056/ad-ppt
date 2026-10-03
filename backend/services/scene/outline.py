"""Bounded outline content shared by manual confirmation and model candidates."""
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .validation import clean_text

OutlineRole = Literal['cover', 'agenda', 'section', 'content', 'closing', 'unknown']
OutlineLine = Annotated[str, Field(strict=True, max_length=1000)]


class OutlinePage(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

    title: str = Field(default='', max_length=500)
    points: list[OutlineLine] = Field(default_factory=list, max_length=20)
    role: OutlineRole = 'unknown'
    facts_needed: list[OutlineLine] = Field(default_factory=list, max_length=20)
    sources: list[OutlineLine] = Field(default_factory=list, max_length=20)

    _title_text = field_validator('title')(clean_text)

    @field_validator('points', 'facts_needed', 'sources')
    @classmethod
    def plain_lines(cls, values):
        return [clean_text(value) for value in values]


class OutlineDraft(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    pages: list[OutlinePage] = Field(min_length=1)


def normalize_outline_page(value):
    page = OutlinePage.model_validate(value)
    # Existing manual outlines keep their old shape; optional facts/roles are
    # retained only when explicitly supplied, not silently invented.
    return {**page.model_dump(exclude_unset=True), 'title': page.title, 'points': page.points}


def normalize_outline_draft(value, expected_count):
    draft = OutlineDraft.model_validate(value)
    if len(draft.pages) != expected_count:
        raise ValueError('Wrong outline page count')
    for page in draft.pages:
        if not {'role', 'title', 'points', 'facts_needed'} <= page.model_fields_set or page.role == 'unknown':
            raise ValueError('Model outline fields incomplete')
    return draft.model_dump()
