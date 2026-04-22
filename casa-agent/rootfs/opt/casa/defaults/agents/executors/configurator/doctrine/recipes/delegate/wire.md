# Recipe: wire a specialist into a resident's delegates

Makes specialist X callable by resident Y.

## Ask the user (only if ambiguous)

Usually a follow-up after creating a specialist.

## Edit agents/<resident-role>/delegates.yaml

Add to the `delegates:` list:

    - agent: <specialist-role>
      purpose: <one-sentence description>
      when: <trigger phrase or criteria>

purpose and when are surfaced in the resident's system prompt - make them specific.

## Also: add the delegate tool to resident's allowed tools

In agents/<resident-role>/runtime.yaml::tools.allowed, ensure mcp__casa-framework__delegate_to_specialist is listed.

## Reload

**Hard** - delegates.yaml is boot-cached.

    config_git_commit(message="wire <specialist> into <resident>'s delegates")
    emit_completion
    casa_reload()
