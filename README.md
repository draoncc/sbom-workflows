# sbom-workflows

Reusable GitHub Actions workflow that creates SPDX and CycloneDX SBOM attestations for every Platform manifest of a repository's GHCR Images that lacks one.

## Usage

Add a workflow to the repository that publishes the Images:

```yaml
name: SBOM attestations

on:
  workflow_dispatch:
  registry_package:
    types: [published]

jobs:
  sbom:
    permissions:
      contents: read
      packages: write
      id-token: write
      attestations: write
      artifact-metadata: write
    uses: draoncc/sbom-workflows/.github/workflows/sbom-attest-all.yaml@main # pin to a commit SHA
    with:
      include-untagged: false
      dry-run: false
```

The caller must grant at least the permissions above; a reusable workflow cannot exceed them.

## Inputs

| Input | Default | Description |
| --- | --- | --- |
| `include-untagged` | `false` | Also attest untagged image versions |
| `dry-run` | `false` | Only report missing attestations in the job summary |
| `max-digests` | `256` | Max subject digests attested per run (capped at 256); the rest wait for the next run |
| `max-parallel` | `4` | Max concurrent attest jobs |
| `syft-version` | `v1.57.2` | Syft version to generate SBOMs with |

Secret `token` (optional) overrides `GITHUB_TOKEN` for packages and registry access.

## Verifying

```sh
gh attestation verify oci://ghcr.io/<owner>/<image>@<digest> --repo <owner>/<repo> \
  --predicate-type https://cyclonedx.org/bom
```
