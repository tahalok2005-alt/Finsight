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

**Note:** storage on a free Space is temporary. Uploaded documents are removed
when the Space restarts or goes to sleep.
