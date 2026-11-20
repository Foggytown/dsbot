"""Counting approved reports per person, and the weekly numbers built on it.

Layout:

  * `period`   - what "this week" means, as explicit UTC instants
  * `counting` - pure rules: report + reactions -> per-person counts
  * `render`   - counts -> Discord text and CSV
  * `service`  - database and roster plumbing around the three above

The split exists so the rule that decides whether a report "counts" can be read and
tested without a database, a clock, or Discord.
"""
