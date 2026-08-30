"""Tesseract engine adapter (optional, not installed on this machine).

Kept as a working second implementation of the ``OCREngine`` interface so the
abstraction is demonstrably real rather than aspirational.  It raises
``EngineUnavailable`` with actionable instructions when the binary or the
``pytesseract`` bridge is missing, and never degrades silently.
"""
from __future__ import annotations

import shutil
from time import perf_counter

import numpy as np

from app.config.settings import settings
from app.core.errors import EngineUnavailable
from app.core.geometry import BBox
from app.core.ids import stable_id
from app.core.logging import get_logger
from app.models.schemas import BBoxModel, OCRElement, PageOCRResult
from app.ocr.base import EngineInfo, OCREngine

log = get_logger("ocr.tesseract")


class TesseractEngine(OCREngine):
    def __init__(self, lang: str = "eng", psm: int = 3):
        self.lang = lang
        self.psm = psm
        self._checked = False
        self._version = "unknown"

    def _ensure(self) -> None:
        if self._checked:
            return
        try:
            import pytesseract  # noqa: PLC0415
        except ImportError as exc:
            raise EngineUnavailable(
                "pytesseract is not installed. Install with: pip install pytesseract"
            ) from exc
        if shutil.which("tesseract") is None:
            raise EngineUnavailable(
                "The 'tesseract' binary is not on PATH. Install Tesseract OCR "
                "(e.g. the UB-Mannheim Windows build) and re-run.")
        try:
            self._version = str(pytesseract.get_tesseract_version())
        except Exception as exc:  # noqa: BLE001
            raise EngineUnavailable(f"Tesseract is present but unusable: {exc}") from exc
        self._checked = True

    @property
    def info(self) -> EngineInfo:
        try:
            self._ensure()
            available = True
        except EngineUnavailable:
            available = False
        return EngineInfo(
            name="tesseract", version=self._version if available else "unavailable",
            languages=(self.lang,), supports_angle_classification=False,
            notes="LSTM engine; requires a system binary. Second implementation "
                  "of the OCREngine interface.")

    def recognize(self, image: np.ndarray, *, document_id: str,
                  page_number: int) -> PageOCRResult:
        self._ensure()
        import pytesseract  # noqa: PLC0415
        from pytesseract import Output  # noqa: PLC0415

        h, w = image.shape[:2]
        t0 = perf_counter()
        data = pytesseract.image_to_data(
            image, lang=self.lang, config=f"--psm {self.psm}",
            output_type=Output.DICT)
        duration_ms = (perf_counter() - t0) * 1000.0

        elements: list[OCRElement] = []
        n = len(data.get("text", []))
        for i in range(n):
            text = (data["text"][i] or "").strip()
            if not text:
                continue
            try:
                conf = float(data["conf"][i]) / 100.0
            except (TypeError, ValueError):
                continue
            if conf < settings.ocr_min_confidence:
                continue
            bbox = BBox(float(data["left"][i]), float(data["top"][i]),
                        float(data["width"][i]), float(data["height"][i])).clipped(w, h)
            if bbox.area <= 0:
                continue
            elements.append(OCRElement(
                id=stable_id("ocr", document_id, page_number, i, text),
                document_id=document_id, page_number=page_number, text=text,
                confidence=conf, bbox=BBoxModel(**bbox.as_dict()),
                block_id=str(data["block_num"][i]), line_id=str(data["line_num"][i]),
                word_id=str(data["word_num"][i]), source="ocr",
                font_size_px=round(bbox.height, 2)))

        mean_conf = (sum(e.confidence for e in elements) / len(elements)
                     if elements else 0.0)
        return PageOCRResult(
            page_number=page_number, engine="tesseract", engine_version=self._version,
            elements=elements, mean_confidence=round(mean_conf, 4),
            duration_ms=round(duration_ms, 1), image_width=w, image_height=h,
            warnings=[] if elements else ["no text recognised on this page"])
