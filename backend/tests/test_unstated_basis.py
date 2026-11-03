"""A slide that names something else where its siblings name a basis.

Found in the Phase 21 corpus (a lender's investor presentation). Slide 37 is
titled "Financial summary - Consolidated"; slide 38, the parent company's own
figures, "Financial summary - Bajaj Finance". Slide 38 declares no basis, so
its figures fell to the document default -- the consolidated group -- and 57
of them were filed there, unflagged. The title band said, in the very place
its sibling names a basis, that this page is something else.
"""
from __future__ import annotations

from app.document.assembly import _basis_by_page
from app.models.enums import RegionType
from app.models.schemas import BBoxModel, Region


def _region(i, page, text, y=10):
    return Region(id=f"r{page}_{i}", document_id="d", page_number=page,
                  type=RegionType.TITLE, bbox=BBoxModel(x=20, y=y, width=1300, height=60),
                  confidence=0.9, reading_order=i, text=text)


def _pages(titles):
    return {page: [_region(0, page, title),
                   _region(1, page, "Interest income 20,513 17,145", y=400)]
            for page, title in titles.items()}


def test_the_sibling_title_makes_the_basis_unstated():
    got = _basis_by_page(_pages({37: "Financial summary - Consolidated",
                                 38: "Financial summary - Bajaj Finance"}),
                         set(), {37: 1500, 38: 1500})
    assert got == {37: "consolidated", 38: "unstated"}


def test_a_page_with_an_unrelated_title_is_left_to_the_usual_rules():
    got = _basis_by_page(_pages({37: "Financial summary - Consolidated",
                                 40: "Asset quality - key metrics"}),
                         set(), {37: 1500, 40: 1500})
    assert got == {37: "consolidated"}


def test_without_a_declaring_sibling_nothing_changes():
    got = _basis_by_page(_pages({38: "Financial summary - Bajaj Finance"}),
                         set(), {38: 1500})
    assert got == {}
