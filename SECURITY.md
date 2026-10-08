# Security Policy

## Supported Versions

Modus is a portfolio project maintained on a best-effort basis. Only the latest
release on the `main` branch receives security fixes.

| Version | Supported |
| ------- | --------- |
| 1.x     | Yes       |
| < 1.0   | No        |

## Reporting a Vulnerability

If you discover a vulnerability in Modus, please report it privately.

**Do not open a public GitHub issue for security vulnerabilities.**

Use GitHub private vulnerability reporting:

<https://github.com/babbguy/Modus/security/advisories/new>

Please include:

- A description of the vulnerability
- Steps to reproduce
- Affected versions or commits
- Potential impact
- A suggested fix, if you have one

### What to expect

This is a volunteer-maintained project, so there are no guaranteed response or
fix times. Reports will be looked at as time allows, confirmed issues will be
fixed as quickly as reasonably possible, and reporters will be credited unless
they prefer to stay anonymous. Please allow time for a fix before any public
disclosure.

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
