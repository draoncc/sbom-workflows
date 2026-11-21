# sbom-workflows

Reusable automation that ensures every container image a repository publishes carries signed SBOM attestations.

## Language

**Image**:
A container package in any registry, owned by a user or org. An Image in GHCR is linked to one Linked repository; only those get SBOM attestations.
_Avoid_: Container, package (when meaning the image itself)

**Linked repository**:
The repository a GHCR Image is linked to; SBOM attestations for its Platform manifests are stored there.
_Avoid_: Source repo, owning repo

**Image version**:
One digest-addressed entry of an Image; tagged or untagged.
_Avoid_: Build, release

**Tagged image version**:
An Image version that has a tag, or is a Platform manifest of an Index that has one. All others are untagged, even if GHCR shows no tag on a Platform manifest.
_Avoid_: Released version

**Index**:
An Image version listing one Platform manifest per OS/architecture.
_Avoid_: Manifest list, multi-arch image

**Platform manifest**:
An Image version describing a single OS/architecture; the only thing SBOM attestations are made for.
_Avoid_: Sub-image, arch image

**Subject digest**:
The digest of a Platform manifest that an attestation is about.
_Avoid_: Image digest, hash

**SBOM format**:
One of SPDX or CycloneDX.

**SBOM attestation**:
A signed statement binding an SBOM in one SBOM format to one Subject digest.
_Avoid_: SBOM (when meaning the signed statement), signature

**Missing attestation**:
A (Subject digest, SBOM format) pair with no SBOM attestation.
_Avoid_: Gap, unattested image

**Flux root**:
A repository path Flux applies directly to a cluster, such as the bootstrap path; everything it deploys is reached from it.
_Avoid_: Root kustomization, cluster dir

**Controller**:
The Flux object (Kustomization or HelmRelease) that deploys a Workload.
_Avoid_: Owner, parent

**Workload**:
A Kubernetes object that runs one or more Images.
_Avoid_: App, service

**Substitution variables**:
KEY=VALUE pairs Flux substitutes into manifests before applying them.
_Avoid_: Var map, env, postBuild vars
