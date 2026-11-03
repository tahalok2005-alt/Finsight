---
title: FinSight
emoji: 📊
colorFrom: blue
colorTo: gray
sdk: docker
app_port: 7860
pinned: false
short_description: Reads financial PDFs into traceable, validated figures
---

# FinSight

Reads company results PDFs — including scanned pages and slides that are only
pictures — and returns each figure with its measure, period, unit, currency,
entity and the exact page region it came from. Anything the document does not
state clearly is flagged for review rather than guessed.

Everything runs inside this Space on CPU. No document is sent to any outside
service, and no language model is used.

Source and documentation: <https://github.com/tahalok2005-alt/Finsight>

**Your documents are private.** Only the browser that uploaded a document can
see it, and every document is deleted 24 hours after upload — sooner if the
Space restarts. There are no accounts: clearing your cookies or switching
browsers starts a new, empty workspace. Limits: 25 MB and 60 pages per file.
