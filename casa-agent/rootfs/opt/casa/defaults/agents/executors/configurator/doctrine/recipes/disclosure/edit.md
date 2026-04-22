# Recipe: edit disclosure policy

policies/disclosure.yaml and per-resident disclosure.yaml control what memory items are disclosed on which channel.

## Ask the user

1. **Policy level or resident-specific?**
2. **What to change?**

## Files

- /addon_configs/casa-agent/policies/disclosure.yaml - global fallback.
- /addon_configs/casa-agent/agents/<resident-role>/disclosure.yaml - per-resident override.

## Format

See schema/policy-disclosure.v1.json and schema/disclosure.v1.json.

## Reload

**Hard** - disclosure rules are boot-cached.
