"""Layout-aware financial extraction.

Reads financial figures that are arranged spatially rather than tabulated:
KPI tiles, metric cards and other presentation layouts where the relationship
between a label and its number is expressed by position instead of by a grid.

Feeds the same canonical ``FinancialFact`` pipeline as table and narrative
extraction -- same taxonomy, same number normalisation, same period resolver,
same confidence scale, same provenance.
"""
