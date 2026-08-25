# ADR 0012: Add opt-in local dashboard authentication

Status: **Accepted for the post-v0.1 local dashboard**

## Decision

The loopback-only read-only dashboard may be started with a local keyring. The
default command without `--keyring` remains unauthenticated. Authentication is
a narrow operator convenience for a local browser session; it is not public
hosting, TLS, multi-user, or SaaS security.

The keyring is a strict canonical JSON file with mode `0600`. It contains only
versioned public metadata and a domain-separated SHA-256 digest of each bearer
token. A token has a version, public key ID, and 256 random secret bits. The
secret is printed exactly once by CLI create/rotate and is never stored,
logged, placed in a URL, or returned by an HTTP endpoint. Keyring create, list,
revoke, and rotate are CLI-only operations protected by an inter-process lock
and atomic replacement.

Protected evidence routes require exactly one `Authorization: Bearer` header
with the active `dashboard:read` token. Health and packaged static assets stay
available without a token so a local user can reach the unlock screen. Missing,
malformed, unknown, revoked, duplicate, query-string, cookie, and Basic
credentials share one bounded `401` response with `WWW-Authenticate: Bearer`.
Runtime keyring corruption is a generic `503` and never becomes an allow.
Revocation and rotation are observed on the next request without restarting
the server.

The browser holds the token only in JavaScript memory. It sends it only as a
same-origin Authorization header, forgets it on reload, and provides a Lock
action. The dashboard does not use local/session storage, cookies, query
parameters, analytics, or console output for the token.

## Operator flow

```bash
# The parent directory must already exist. The secret appears once in stdout.
uv run inferdrome dashboard-keyring create \
  --keyring .local/dashboard-keyring.json --label workstation

uv run inferdrome dashboard \
  --keyring .local/dashboard-keyring.json \
  --runs-root runs --trial-sets-root trial-sets \
  --comparison-plans-root comparison-plans \
  --comparison-results-root comparison-results

# Metadata only; never prints a digest or secret.
uv run inferdrome dashboard-keyring list \
  --keyring .local/dashboard-keyring.json

# Rotate atomically; the replacement secret appears once.
uv run inferdrome dashboard-keyring rotate \
  --keyring .local/dashboard-keyring.json \
  --revoke-key-id dk-0123456789abcdef --label workstation-rotated

# Revocation is idempotent for an existing key and never prints its secret.
uv run inferdrome dashboard-keyring revoke \
  --keyring .local/dashboard-keyring.json dk-0123456789abcdef
```

The keyring is local operator state, not evidence, a deployment receipt, an
Inferdrome API key, or a credential-management service. Keep it outside the
repository, evidence bundles, container images, and shared filesystems. The
supported server remains bound to `127.0.0.1`; remote access, TLS, tenancy,
rate limiting, and multi-user authorization require a separate security ADR.
