# sbom-workflows

Reusable GitHub Actions workflows for a repository's container Images:

- [`sbom-attest-all`](#sbom-attest-all) creates SPDX and CycloneDX SBOM attestations for every Platform manifest of the repository's GHCR Images that lacks one.
- [`flux-image-map`](#flux-image-map) prints every Image the repository's Flux roots deploy, with its Controller and Workload.

## sbom-attest-all

### Usage

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
      images: | # defaults to the repo name
        my-app
        my-app/worker
      include-untagged: false
      dry-run: false
```

The caller must grant at least the permissions above; a reusable workflow cannot exceed them.

### Inputs

| Input | Default | Description |
| --- | --- | --- |
| `images` | repo name | Newline-separated Image names under the owner (`ghcr.io/<owner>/<name>`). Each must be linked to the calling repo; a named Image that is missing or linked elsewhere fails the run, a missing default only warns |
| `include-untagged` | `false` | Also attest untagged image versions |
| `dry-run` | `false` | Only report missing attestations in the job summary |
| `max-digests` | `256` | Max subject digests attested per run (capped at 256); the rest wait for the next run |
| `max-parallel` | `4` | Max concurrent attest jobs |
| `syft-version` | `v1.57.2` | Syft version to generate SBOMs with |

Secret `token` (optional) overrides `GITHUB_TOKEN` for packages and registry access. Images are never listed, only looked up by name, since `GITHUB_TOKEN` cannot list packages (see [ADR 0001](docs/adr/0001-name-images-explicitly.md)).

### Verifying

```sh
gh attestation verify oci://ghcr.io/<owner>/<image>@<digest> --repo <owner>/<repo> \
  --predicate-type https://cyclonedx.org/bom
```

## flux-image-map

Renders the repository's Flux roots the way Flux would apply them and prints every deployed Image with its Controller and Workload as a table, in the job log and the step summary. It only reports: nothing is attested, pushed or exported.

### Usage

Add a workflow to the repository that holds the Flux manifests:

```yaml
name: Flux image map

on:
  workflow_dispatch:
  pull_request:
    paths: [clusters/**, apps/**]

jobs:
  images:
    permissions:
      contents: read
      packages: read
    uses: draoncc/sbom-workflows/.github/workflows/flux-image-map.yaml@main # pin to a commit SHA
    with:
      root: | # Flux bootstrap path(s)
        clusters/prod
        clusters/staging
      vars: | # what the cluster's postBuild substitutions would provide
        REGISTRY=ghcr.io/my-org
        APP_TAG=1.4.2
      resolve-digests: true
```

The caller must grant at least the permissions above; a reusable workflow cannot exceed them.

Set `root` to the Flux bootstrap path(s). The default `.` renders the whole repository as Flux would a Kustomization with `path: ./`, which fails if the repository holds YAML that isn't a Kubernetes manifest (Helm chart templates, CI config, fixtures). Each root's Flux Kustomizations are followed to their local paths, so a bootstrap path is enough to reach everything it deploys.

### Inputs

| Input | Default | Description |
| --- | --- | --- |
| `root` | `.` | Flux roots to render, one path per line, relative to the repository root |
| `vars` | `""` | Substitution variables, one `KEY=VALUE` per line; a `${VAR}` without a value is kept literally |
| `kube-version` | `""` | Kubernetes version passed to `helm template` |
| `resolve-digests` | `false` | Add a digest column, looked up in each Image's registry |
| `strict` | `false` | Fail on any fallback or warning |

Secret `token` (optional) overrides `GITHUB_TOKEN` for ghcr.io access when resolving digests. Images in other registries are looked up anonymously.

### Output

```
IMAGE                               DIGEST           CONTROLLER                WORKLOAD                     SOURCE     ROOT
ghcr.io/my-org/app:1.4.2            sha256:3f1c…     Deployment/apps/app       Deployment/apps/app          kustomize  apps/app
docker.io/org/proxy:2.1             -                Deployment/apps/app       Deployment/apps/app          kustomize  apps/app
ghcr.io/stefanprodan/podinfo:6.7.0  sha256:9a7e…     HelmRelease/apps/podinfo  Deployment/apps/podinfo      helm       apps/podinfo
```

`DIGEST` (shortened here) appears only with `resolve-digests`, and is `-` when the lookup fails. `SOURCE` is `kustomize` or `helm`, or `helm-unrendered` when a chart couldn't be fetched and the row comes from the HelmRelease alone. The job fails when a root can't be rendered, and with `strict` also on any warning or unresolved `${VAR}`.
