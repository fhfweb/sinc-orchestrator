# Security Rules

Project-specific security standards.
All agents must be aware of these rules.
See also /docs/skills/security.md for general security engineering patterns.

---

## Authentication Standards

- Auth method: [e.g. Laravel Sanctum / JWT / OAuth 2.0]
- Token TTL: [e.g. 24 hours for access tokens, 30 days for refresh tokens]
- Session TTL: [e.g. 7 days with sliding expiration]
- Password requirements: [e.g. minimum 8 chars, complexity rules]
- Failed login lockout: [e.g. 5 attempts -> 15 min lockout]
- MFA: [required / optional / enterprise-only]

---

## Authorization Standards

- Model: [RBAC / ABAC / custom]
- Roles: [list all roles with descriptions]
- Default permission: deny all (explicit grant required)
- Tenant isolation: [how multi-tenancy is enforced — if applicable]

---

## Data Classification

| Level | Description | Examples | Controls |
|-------|-------------|---------|----------|
| Public | Visible to anyone | Product pages | None |
| Internal | Visible to authenticated users | Dashboard data | Auth required |
| Sensitive | Restricted to specific roles | Billing, PII | Role check + audit log |
| Secret | Encrypted, minimal access | API keys, tokens | Encrypted + vault |

---

## Sensitive Data Handling

Fields that must be encrypted at rest:
- [field 1]: reason
- [field 2]: reason

Fields that must NEVER appear in logs:
- passwords
- tokens
- credit card numbers
- [any project-specific fields]

---

## API Security Rules

- Rate limits:
  - Auth endpoints: [X requests per minute per IP]
  - General API: [X requests per minute per user]
  - Heavy operations: [X requests per hour per user]
- CORS: [which origins are allowed]
- Required headers: [e.g. X-Request-ID for tracing]

---

## Dependency Policy

- New dependencies require review before addition
- All dependencies must be scanned for CVEs before merge
- Dependencies with known critical CVEs must be updated within [X days]
- No abandoned packages (last updated > 2 years ago) without justification

---

## Incident Response Contacts

- Security issues should be logged in /security/audit.md
- P0 security incidents: [escalation procedure]
- Disclosure policy: [responsible disclosure approach]

---

## Compliance Requirements

[List any regulatory requirements — GDPR, SOC2, HIPAA, PCI-DSS, etc.]
- [Requirement]: [what it means for the engineering team]
