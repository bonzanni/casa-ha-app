# Recipe: wire an agent into a resident's delegates

Makes agent X (resident or specialist) callable by resident Y.

## Ask the user (only if ambiguous)

Usually a follow-up after creating a specialist, or when wiring one
resident to delegate to another (e.g. Ellen → Tina for device control).

## Edit agents/<resident-role>/delegates.yaml

Add to the `delegates:` list:

    - agent: <target-role>          # resident OR specialist role
      purpose: <one-sentence description>
      when: <trigger phrase or criteria>

purpose and when are surfaced in the resident's system prompt - make them specific.

## Also: add the delegate tool to resident's allowed tools

In agents/<resident-role>/runtime.yaml::tools.allowed, ensure mcp__casa-framework__delegate_to_agent is listed.

## Reload — MANDATORY before emit_completion

**Hard** - delegates.yaml is boot-cached. Canonical order:

    config_git_commit(message="wire <target> into <resident>'s delegates")
    casa_reload()
    emit_completion(status="ok", text="...committed SHA <sha>, called casa_reload for delegates rebuild.")
