"""OCR engine registry.

Engines register themselves here; the pipeline resolves one by name from
settings.  Adding a stronger recogniser later means writing one class and one
``register`` call -- no pipeline change.
"""
from __future__ import annotations

from typing import Callable

from app.config.settings import settings
from app.core.errors import EngineUnavailable
from app.core.logging import get_logger
from app.ocr.base import OCREngine

log = get_logger("ocr.registry")

_REGISTRY: dict[str, Callable[[], OCREngine]] = {}
_SINGLETONS: dict[str, OCREngine] = {}


def register(name: str, factory: Callable[[], OCREngine]) -> None:
    _REGISTRY[name] = factory


def available_engines() -> list[str]:
    return sorted(_REGISTRY)


def probe_engines() -> dict[str, dict]:
    """Report which registered engines can actually be constructed here."""
    out: dict[str, dict] = {}
    for name, factory in _REGISTRY.items():
        try:
            eng = factory()
            info = eng.info
            # An engine reports itself unavailable by leaving the version blank
            # or by raising from .info; both are treated as "cannot be used".
            usable = info.version not in ("", "unavailable")
            out[name] = {"available": usable, "version": info.version,
                         "languages": list(info.languages), "notes": info.notes}
            if not usable:
                out[name]["error"] = "engine dependencies are not installed"
        except Exception as exc:  # noqa: BLE001 - probing must not raise
            out[name] = {"available": False, "error": str(exc)}
    return out


def get_engine(name: str | None = None) -> OCREngine:
    """Return a cached engine instance (model load is expensive)."""
    name = name or settings.ocr_engine
    if name in _SINGLETONS:
        return _SINGLETONS[name]
    if name not in _REGISTRY:
        raise EngineUnavailable(
            f"Unknown OCR engine '{name}'. Available: {', '.join(available_engines())}")
    engine = _REGISTRY[name]()
    _SINGLETONS[name] = engine
    return engine


def reset() -> None:
    for eng in _SINGLETONS.values():
        try:
            eng.close()
        except Exception:  # noqa: BLE001
            pass
    _SINGLETONS.clear()


def _register_builtin() -> None:
    from app.ocr.rapidocr_engine import RapidOCREngine  # noqa: PLC0415
    register("rapidocr", RapidOCREngine)
    try:
        from app.ocr.tesseract_engine import TesseractEngine  # noqa: PLC0415
        register("tesseract", TesseractEngine)
    except Exception:  # noqa: BLE001 - optional
        pass


_register_builtin()
