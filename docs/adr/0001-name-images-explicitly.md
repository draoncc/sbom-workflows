# Callers name their Images instead of the workflow discovering them

The default `GITHUB_TOKEN` cannot list an owner's packages (the REST list endpoint needs a classic PAT; GraphQL no longer exposes GHCR), but it can look up a package by name when the package's Linked repository is the calling repo. So the workflow takes Image names from the `images` input, defaulting to the repo name, and verifies each one's Linked repository rather than listing and filtering.

## Considered Options

- **PAT or listing when the `token` secret is set**: rejected; one discovery mechanism regardless of token, and no secret should be required.
- **GitHub App installation token**: needs an App per org, and whether it can list container packages is undocumented.
- **Scan the repo's workflows for pushed image names**: fragile against expressions, matrices and non-Docker build tools.
- **`registry_package` event payload**: covers one Image per event and does not fire for pushes made with `GITHUB_TOKEN`.
