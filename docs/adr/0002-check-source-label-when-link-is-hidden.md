# Check the source label when the package API hides the Linked repository

The package API returns `repository: null` to `GITHUB_TOKEN` even for an Image whose Linked repository is the calling repo; a classic PAT sees it. A successful lookup only proves the calling repo has access to the Image, not that it is linked. So when `repository` is null, each Platform manifest with a Missing attestation is attested only if its `org.opencontainers.image.source` (manifest annotation, else config label) is the calling repo, the same label GitHub links Images by. The manifest annotation comes first because GitHub also links multi-arch Images by annotation. A named Image fails the run if none of its checked Platform manifests match, including when none of their labels can be read. A non-null `repository` is still compared as before.

This amends ADR 0001, which assumed a lookup succeeds only when the calling repo is the Linked repository. Anyone who can push the Image controls its label, so this check is as strong as GitHub's own label-based linking and no stronger.

## Considered Options

- **Accept a successful lookup as linked**: rejected; access can be granted to repos the Image is not linked to, and their attestations would land in the wrong repo.
- **Check one label per Image**: rejected; the label is per Image version, so older versions may name another repo or none.
- **Require the `token` secret**: rejected for the same reason as in ADR 0001.
