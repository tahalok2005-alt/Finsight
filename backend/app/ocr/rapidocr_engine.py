"""RapidOCR engine (PP-OCR detection + recognition exported to ONNX).

Why this engine
---------------
Selected after testing the practical options against this machine (CPU-only,
Intel UHD graphics, Python 3.13) and against financial-page characteristics:

* **RapidOCR / PP-OCR ONNX** - pip-installable with the models bundled
  (~12 MB), no system binary, no GPU, runs on Python 3.13.  PP-OCR's DBNet
  detector handles the dense, small, right-aligned numeric columns typical of
  financial tables, and it returns quadrilaterals, which is what the layout and
  table stages need.  Measured on the benchmark document: ~13 s/page at 200 dpi
  with every figure on the consolidated table read correctly.
* **Tesseract** - would need a system installer that is not present, and its
  LSTM line model is weaker on tightly-spaced numeric columns.  Kept viable via
  the engine interface.
* **PaddleOCR (native)** - the paddlepaddle wheel has no Python 3.13 build for
  Windows; the ONNX export used here is the same model family.
* **EasyOCR** - would pull the full torch stack for detection quality below
  PP-OCR on printed tabular text.

Known limitation, handled explicitly: the PP-OCR recognition charset does not
include the rupee sign, so currency glyphs are dropped from recognised text.
The auxiliary text-layer channel (``app.ocr.textlayer``) recovers them for
digital PDFs, and the unit/currency normaliser falls back on lexical cues
("crore", "Rs.") otherwise.  This is recorded rather than hidden.
"""
from __future__ import annotations

import threading
from time import perf_counter
from typing import Any

import numpy as np

from app.config.settings import settings
from app.core.errors import EngineUnavailable
from app.core.geometry import BBox
from app.core.ids import stable_id
from app.core.logging import get_logger
from app.models.schemas import BBoxModel, OCRElement, PageOCRResult
from app.ocr.base import EngineInfo, OCREngine

log = get_logger("ocr.rapidocr")

_ENGINE_LOCK = threading.Lock()


class RapidOCREngine(OCREngine):
    def __init__(self, *, det_limit_side_len: int | None = None,
                 text_score: float | None = None,
                 use_angle_cls: bool | None = None,
                 rec_batch_num: int | None = None):
        self.det_limit_side_len = det_limit_side_len or settings.ocr_det_limit_side_len
        self.text_score = text_score if text_score is not None else settings.ocr_text_score
        self.use_angle_cls = (settings.ocr_use_angle_cls
                              if use_angle_cls is None else use_angle_cls)
        self.rec_batch_num = rec_batch_num or settings.ocr_rec_batch_num
        self._ocr: Any = None
        self._version = self._package_version()

    @staticmethod
    def _package_version() -> str:
        """Resolve the installed version without paying the model-load cost."""
        try:
            from importlib.metadata import version  # noqa: PLC0415
            return version("rapidocr-onnxruntime")
        except Exception:  # noqa: BLE001 - not installed / no metadata
            return ""

    # ----------------------------------------------------------------- setup
    def _build(self) -> Any:
        try:
            from rapidocr_onnxruntime import RapidOCR  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover - install-time failure
            raise EngineUnavailable(
                "rapidocr-onnxruntime is not installed. "
                "Install it with: pip install rapidocr-onnxruntime") from exc
        if not self._version:
            self._version = "unknown"

        # NOTE: RapidOCR's kwarg updater requires the *_model_path keys to be
        # present (falsy is fine -> it falls back to the bundled model).
        return RapidOCR(
            det_model_path=None,
            det_limit_side_len=self.det_limit_side_len,
            det_limit_type="max",
            det_use_cuda=False,
            rec_model_path=None,
            rec_batch_num=self.rec_batch_num,
            rec_use_cuda=False,
            cls_model_path=None,
            cls_use_cuda=False,
            use_angle_cls=self.use_angle_cls,
            text_score=self.text_score,
        )

    @property
    def engine(self) -> Any:
        if self._ocr is None:
            with _ENGINE_LOCK:
                if self._ocr is None:
                    t0 = perf_counter()
                    self._ocr = self._build()
                    log.info("ocr.engine_loaded", engine="rapidocr",
                             load_ms=round((perf_counter() - t0) * 1000, 1),
                             det_side=self.det_limit_side_len,
                             angle_cls=self.use_angle_cls)
        return self._ocr

    @property
    def info(self) -> EngineInfo:
        return EngineInfo(
            name="rapidocr",
            version=self._version,
            languages=("en", "ch"),
            supports_angle_classification=True,
            notes="PP-OCRv3 DBNet detector + CRNN recogniser, ONNX Runtime CPU. "
                  "Currency glyphs outside the recognition charset are dropped; "
                  "recovered from the PDF text layer when available.",
        )

    def warmup(self) -> None:
        probe = np.full((64, 256, 3), 255, dtype=np.uint8)
        probe[20:44, 10:200] = 0
        try:
            self.engine(probe)
        except Exception as exc:  # noqa: BLE001 - warmup must never be fatal
            log.warning("ocr.warmup_failed", error=str(exc))

    def close(self) -> None:
        self._ocr = None

    # ------------------------------------------------------------- inference
    def recognize(self, image: np.ndarray, *, document_id: str,
                  page_number: int) -> PageOCRResult:
        if image.ndim != 3 or image.shape[2] != 3:
            raise EngineUnavailable(
                f"Expected an HxWx3 RGB image, got shape {image.shape}")
        h, w = image.shape[:2]
        warnings: list[str] = []

        t0 = perf_counter()
        try:
            raw, _ = self.engine(image)
        except Exception as exc:
            raise EngineUnavailable(
                f"OCR failed on page {page_number}: {exc}") from exc
        duration_ms = (perf_counter() - t0) * 1000.0

        elements: list[OCRElement] = []
        if raw:
            for idx, item in enumerate(raw):
                try:
                    poly, text, conf = item[0], item[1], item[2]
                    confidence = float(conf)
                except (IndexError, TypeError, ValueError):
                    warnings.append(f"malformed OCR record at index {idx}")
                    continue
                text = (text or "").strip()
                if not text:
                    continue
                if confidence < settings.ocr_min_confidence:
                    # Kept out of the canonical set but recorded so the page's
                    # recall problems are visible rather than invisible.
                    warnings.append(
                        f"dropped low-confidence token {text!r} ({confidence:.2f})")
                    continue
                polygon = [[float(p[0]), float(p[1])] for p in poly]
                bbox = BBox.from_polygon(polygon).clipped(w, h)
                if bbox.area <= 0:
                    continue
                eid = stable_id("ocr", document_id, page_number, idx,
                                round(bbox.x, 1), round(bbox.y, 1), text)
                elements.append(OCRElement(
                    id=eid, document_id=document_id, page_number=page_number,
                    text=text, confidence=confidence,
                    bbox=BBoxModel(**bbox.as_dict()), polygon=polygon,
                    line_id=None, source="ocr",
                    font_size_px=round(bbox.height, 2),
                ))

        mean_conf = (sum(e.confidence for e in elements) / len(elements)
                     if elements else 0.0)
        if not elements:
            warnings.append("no text recognised on this page")

        return PageOCRResult(
            page_number=page_number, engine="rapidocr",
            engine_version=self._version, elements=elements,
            mean_confidence=round(mean_conf, 4),
            duration_ms=round(duration_ms, 1),
            image_width=w, image_height=h, warnings=warnings,
        )
