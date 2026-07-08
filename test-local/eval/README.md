# Memory-accuracy eval scripts

Phase-0/0.1 measurement harness from the 2026-06 memory-accuracy project, kept as
regression guards. They run against a live Casa + Hindsight instance (see each
script's header for the required options/env). Outcome of the original
measurement: extraction 100%, and the feared cross-domain pollution did not
manifest at small bank scale — re-run these when the `casa` bank grows 10–100×.

- `phase0_memory_baseline.py` — extraction/recall baseline per access tier
- `phase0_1_pollution_rank.py` — cross-domain pollution ranking + recall-budget sweep
- `context7_keytest.py` — probe: context7 MCP key wiring through add-on options
