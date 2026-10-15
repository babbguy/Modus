# Verifying releases

Starting with v1.2.0, every Modus release is signed and comes with build
provenance, so you can check that what you run was built by this repository's
release workflow from a release tag, and not changed since.

| Artifact | What you can verify |
|----------|---------------------|
| `ghcr.io/babbguy/modus-orchestrator`, `ghcr.io/babbguy/modus-conductor` | cosign signature (Sigstore keyless), SLSA build provenance and an SPDX SBOM, all tied to `.github/workflows/publish.yml` running for a `v*` tag |
| `modus-agent` on PyPI | PEP 740 provenance attestation from `.github/workflows/publish-pypi.yml`, uploaded through PyPI trusted publishing |

Releases up to and including v1.1.0 were not signed.

## Container images

Every image is signed by digest, so a signature covers every tag that points at
that digest. The examples use the orchestrator at version 1.2.0; substitute the
image and version you deploy.

### Signature (cosign)

Requires [cosign](https://docs.sigstore.dev/cosign/system_config/installation/)
3.0 or newer.

```bash
cosign verify ghcr.io/babbguy/modus-orchestrator:1.2.0 \
  --certificate-identity-regexp '^https://github\.com/babbguy/Modus/\.github/workflows/publish\.yml@refs/tags/v' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

The identity must be the publish workflow of `babbguy/Modus` running for a
release tag; a build from a branch or a fork fails this check. To pin the exact
release instead, use
`--certificate-identity https://github.com/babbguy/Modus/.github/workflows/publish.yml@refs/tags/v1.2.0`.

### Provenance and SBOM (GitHub artifact attestations)

Requires the [GitHub CLI](https://cli.github.com/) (`gh attestation`).

```bash
# SLSA build provenance
gh attestation verify oci://ghcr.io/babbguy/modus-orchestrator:1.2.0 -R babbguy/Modus

# SPDX SBOM
gh attestation verify oci://ghcr.io/babbguy/modus-orchestrator:1.2.0 -R babbguy/Modus \
  --predicate-type https://spdx.dev/Document/v2.3
```

Add `--source-ref refs/tags/v1.2.0` to require the exact tag. To read the SBOM
itself, add `--format json --jq '.[].verificationResult.statement.predicate'`.
The attestations are also stored in the registry next to the image, so
`--bundle-from-oci` verifies them without calling the GitHub API.

### Deploy by digest

A tag can be moved; a digest cannot. After verifying, you can deploy the exact
image you checked:

```bash
docker buildx imagetools inspect ghcr.io/babbguy/modus-orchestrator:1.2.0
# take the "Digest:" line and use ghcr.io/babbguy/modus-orchestrator@sha256:<digest>
```

## Python SDK (PyPI)

PyPI shows the provenance of each file on the package's
[file listing](https://pypi.org/project/modus-agent/#files). To check it
locally with [pypi-attestations](https://github.com/pypi/pypi-attestations):

```bash
pip install pypi-attestations
pypi-attestations verify pypi --repository https://github.com/babbguy/Modus \
  pypi:modus_agent-1.2.0-py3-none-any.whl
```

## Base images

The Dockerfiles pin their base image (`python:3.12-slim`) and the Compose files
pin third-party images (PostgreSQL, Caddy) by digest. Dependabot proposes the
new digest when upstream rebuilds a tag.
