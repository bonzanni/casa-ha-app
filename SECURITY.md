# Security policy

## Supported versions

Casa is pre-1.0: only the latest released version receives fixes.

## Reporting a vulnerability

Please report vulnerabilities privately via GitHub's
[private vulnerability reporting](https://github.com/bonzanni/casa-ha-app/security/advisories/new)
(Security tab → *Report a vulnerability*). Please do not open a public issue
for security problems.

Include the affected version and steps to reproduce where possible. You can
expect an initial response within a few days.

## Scope notes

Casa runs Claude-driven agents with real tool access inside your Home
Assistant. Reports about escaping the configured containment — workspace
isolation, tool allowlists, the ingress/webhook auth perimeter, secret
redaction — are explicitly in scope and appreciated, alongside conventional
vulnerabilities.
