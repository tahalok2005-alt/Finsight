"""OCR engine abstraction.

The pipeline depends on this interface, never on a concrete engine, so a better
recogniser can be dropped in without touching layout, tables or extraction.
An engine must return spatially-anchored elements -- a flat string is not an
acceptable OCR result for this system.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Protocol

import numpy as np

from app.models.schemas import PageOCRResult


@dataclass(frozen=True, slots=True)
class EngineInfo:
    name: str
    version: str
    languages: tuple[str, ...]
    supports_angle_classification: bool
    notes: str = ""


class OCREngine(abc.ABC):
    """Base class for OCR engines."""

    @property
    @abc.abstractmethod
    def info(self) -> EngineInfo: ...

    @abc.abstractmethod
    def recognize(self, image: np.ndarray, *, document_id: str,
                  page_number: int) -> PageOCRResult:
        """Recognise text in an HxWx3 uint8 RGB page image."""

    def warmup(self) -> None:
        """Optional: run a tiny inference so the first real page isn't slow."""

    def close(self) -> None:
        """Optional: release model resources."""


class OCREngineFactory(Protocol):
    def __call__(self) -> OCREngine: ...
