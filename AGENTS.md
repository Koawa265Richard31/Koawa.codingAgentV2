# KoawaAgent V2 agent rules

This directory is an independent Python coding-agent runtime built as a 15-day sequence of executable slices.

- All V2 production code, tests, migrations, and documentation stay under `v2/**`.
- Do not import from or modify the legacy Java project under the repository root.
- Complete one numbered day/slice at a time; do not add empty modules for future days.
- Use Python 3.12+ and prefer the standard library unless a later slice approves a dependency.
- Every state-changing command must be represented by a typed event and guarded by an exact expected stream version.
- Persist JSON only. Never persist Python objects with `pickle`, runtime clients,
  subprocess handles, credential fields, hidden reasoning, or complete environment
  dumps. User-controlled text is untrusted and needs the later redaction policy.
- External side effects are not exactly-once. Later tool execution must use a ledger and represent uncertain outcomes explicitly.
- Run `python -m unittest discover -s tests -v` from `v2/` with `PYTHONPATH=src` before completing a slice.
