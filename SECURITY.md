# Security Policy

## Supported Versions

Modus is maintained by one person on a best-effort basis. Security fixes go
into a new release of the latest minor version; older versions do not receive
fixes, so upgrade to the latest release to get them.

| Version | Supported |
| ------- | --------- |
| 1.1.x   | Yes       |
| < 1.1   | No        |

## Reporting a Vulnerability

**Do not open a public GitHub issue, discussion or pull request for a
security vulnerability.**

Report it privately through GitHub's private vulnerability reporting:

<https://github.com/babbguy/Modus/security/advisories/new>

(or **Security** tab, then **Report a vulnerability**). Only the maintainer
can see the report. Please include:

- A description of the vulnerability and its potential impact
- Steps to reproduce, or a proof of concept
- Affected versions or commits
- A suggested fix, if you have one

### What to expect

This is a single-maintainer project, so there are no guaranteed response or
fix times. The aim is to acknowledge a report within a week and to keep you
informed while it is investigated. Confirmed issues are fixed in a new release
and published as a GitHub security advisory, with a CVE where one applies.
Reporters are credited unless they prefer to stay anonymous. Please allow time
for a fix to be released before any public disclosure.

## Verifying releases

From v1.2.0, the container images are signed with cosign (Sigstore keyless)
and carry SLSA build provenance and an SPDX SBOM as GitHub artifact
attestations; the `modus-agent` package on PyPI carries a provenance
attestation. For example:

```bash
cosign verify ghcr.io/babbguy/modus-orchestrator:1.2.0 \
  --certificate-identity-regexp '^https://github\.com/babbguy/Modus/\.github/workflows/publish\.yml@refs/tags/v' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com

gh attestation verify oci://ghcr.io/babbguy/modus-orchestrator:1.2.0 -R babbguy/Modus
```

See [docs/verifying-releases.md](docs/verifying-releases.md) for the SBOM, the
PyPI package and deploying by digest.

## Security best practices when deploying Modus

1. Set `MODUS_ENCRYPTION_KEY` so stored credentials are encrypted.
2. Rotate API keys periodically.
3. Terminate TLS in front of the orchestrator (reverse proxy with a certificate).
4. Restrict CORS origins in production (`MODUS_CORS_ORIGINS`).
5. Never run `MODUS_AUTH_MODE=stub` outside local development. It disables
   authentication, and the orchestrator refuses to start with it unless
   `MODUS_ENVIRONMENT=development` (an explicit `MODUS_ALLOW_INSECURE_AUTH=1`
   override exists for isolated test setups; do not use it in a deployment).
6. In `jwt` mode, set a strong `MODUS_JWT_SECRET`. Treat `MODUS_MASTER_API_KEY`
   like a root password: sent as `X-Modus-APIKey`, it authenticates as a
   platform administrator in both auth modes (used by the policy CLI, the
   GitHub Action, the seed script and the dashboard sign-in) and is required to
   register apps, rotate app keys and issue team registration tokens. Store it
   in a secret manager, give it only to automation that needs it, and rotate it
   if it may have leaked. The orchestrator refuses the built-in placeholder key
   when `MODUS_ENVIRONMENT=production`.
7. Outbound calls are opt-in. In particular, AI-written insight text is off by
   default and is only sent to Anthropic when you both enable it in the system
   settings and configure your own key (see the README privacy table).
8. Set `MODUS_ENVIRONMENT=production` to disable the interactive API docs.

## Scope notes on experimental modules

Several modules are experimental or research-grade and should not be relied on
as security controls. In particular:

- Enforcement attestations are signed with HMAC-SHA-512 by default. This is a
  symmetric MAC (tamper evidence), not a publicly verifiable signature. ML-DSA
  signatures are available only with the optional `pqcrypto` package.
- "Trajectory compliance receipts" are SHA-256 hash-chain commitments, not
  zero-knowledge proofs.
- The neuromorphic and eBPF modules are simulations or advisory trackers, not
  hardware or kernel enforcement.
- Threshold approval ("swarm governance") is k-of-n sign-off, not
  privacy-preserving multi-party computation.

See the module docstrings under `orchestrator/core/` for the exact scope of each.
