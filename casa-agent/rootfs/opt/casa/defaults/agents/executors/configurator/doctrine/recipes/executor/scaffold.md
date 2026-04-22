# Recipe: create a new executor type

**Not in scope for v0.12.0.** Creating a new Tier 3 executor type (other than configurator) lands in future plans:

- ha-developer -> Plan 4 (v0.13.0)
- plugin-developer -> Plan 5 (requires claude_code driver spike)

If the user asks you to create a new executor type in v0.12.0:

1. Explain the scope - you're the first executor type; framework ships with plans for ha-developer and plugin-developer.
2. Check if the user's actual need is a specialist (Tier 2). Usually it is.
3. If they genuinely want a new executor type, emit completion status="partial" describing the deferral. Do NOT create a stub - ExecutorRegistry loads every subdirectory and a half-filled one will break load.
