# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml", "semver"]
# ///
"""Map every container image deployed from Flux Kustomization roots to its controller."""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol, TextIO

import semver
import yaml

Doc = dict[str, Any]


def ref(doc: Doc) -> dict[str, str | None]:
    meta = doc.get("metadata") or {}
    return {"kind": doc.get("kind"), "namespace": meta.get("namespace"), "name": meta.get("name")}


def normalize_image(image: str) -> str:
    name, _, digest = image.partition("@")
    first, _, rest = name.partition("/")
    if not rest or not ("." in first or ":" in first or first == "localhost"):
        name = f"docker.io/{name}" if rest else f"docker.io/library/{name}"
    elif first == "docker.io" and "/" not in rest:
        name = f"docker.io/library/{rest}"
    if ":" not in name.rsplit("/", 1)[-1] and not digest:
        name += ":latest"
    return f"{name}@{digest}" if digest else name


def pod_spec(doc: Doc) -> Doc | None:
    spec = doc.get("spec") or {}
    match doc.get("kind"):
        case "Pod":
            return spec
        case "Deployment" | "StatefulSet" | "DaemonSet" | "ReplicaSet" | "Job":
            return (spec.get("template") or {}).get("spec")
        case "CronJob":
            return (((spec.get("jobTemplate") or {}).get("spec") or {}).get("template") or {}).get("spec")
    return None


def image_keys(node: Any) -> list[str]:
    if isinstance(node, dict):
        return [
            image
            for key, value in node.items()
            for image in ([value] if key == "image" and isinstance(value, str) else image_keys(value))
        ]
    if isinstance(node, list):
        return [image for item in node for image in image_keys(item)]
    return []


def extract_images(docs: list[Doc]) -> list[tuple[Doc, str, str]]:
    """Return (workload, image, "pod" | "generic") triples found in rendered manifests.

    HelmReleases are skipped: their values are only images once templated.
    """
    found = []
    for doc in docs:
        if doc.get("kind") == "HelmRelease":
            continue
        spec = pod_spec(doc)
        if spec is None:
            found += [(doc, image, "generic") for image in image_keys(doc)]
            continue
        for key in ("initContainers", "containers", "ephemeralContainers"):
            for container in spec.get(key) or []:
                if isinstance(container.get("image"), str):
                    found.append((doc, container["image"], "pod"))
    return found


COMPARATOR = re.compile(
    r"(!=|>=|<=|>|<|~>|~|\^|=)?\s*v?(\*|[xX]|\d+)(?:\.(\*|[xX]|\d+))?(?:\.(\*|[xX]|\d+))?"
    r"(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?"
)


def parse_version(version: str) -> semver.Version | None:
    try:
        return semver.Version.parse(version.removeprefix("v"))
    except ValueError:
        return None


def comparator(op: str, parts: list[str | None], pre: str | None) -> Callable[[semver.Version], bool]:
    """A Masterminds-style semver comparator, as Flux uses for chart versions."""
    numbers = [int(part) for part in parts if part is not None and part not in "*xX"]
    if not numbers:
        return lambda v: op not in ("!=", ">", "<")
    major, minor, patch = (numbers + [0, 0])[:3]
    lo = semver.Version(major, minor, patch, prerelease=pre)
    base = semver.Version(major, minor, patch)
    hi = (base.bump_major, base.bump_minor, base.bump_patch)[len(numbers) - 1]()
    if op in ("~", "~>"):
        hi = base.bump_major() if len(numbers) == 1 else base.bump_minor()
    if op == "^":
        index = next((i for i, n in enumerate(numbers) if n != 0), len(numbers) - 1)
        hi = (base.bump_major, base.bump_minor, base.bump_patch)[index]()
    exact = len(numbers) == 3
    match op:
        case "" | "=" | "~" | "~>" | "^":
            return (lambda v: v == lo) if exact and op in ("", "=") else (lambda v: lo <= v < hi)
        case "!=":
            return (lambda v: v != lo) if exact else (lambda v: not lo <= v < hi)
        case ">":
            return (lambda v: v > lo) if exact else (lambda v: v >= hi)
        case ">=":
            return lambda v: v >= lo
        case "<":
            return lambda v: v < lo
        case "<=":
            return (lambda v: v <= lo) if exact else (lambda v: v < hi)
    raise ValueError(op)


def resolve_version(constraint: str, versions: list[str]) -> str | None:
    """The highest of versions satisfying constraint, or None."""
    groups = []
    for group in (constraint.strip() or "*").split("||"):
        matches = list(COMPARATOR.finditer(group))
        checks = [comparator(m[1] or "", [m[2], m[3], m[4]], m[5]) for m in matches]
        groups.append((checks, any(m[5] for m in matches)))

    def satisfies(v: semver.Version) -> bool:
        return any(all(check(v) for check in checks) and (allow_pre or not v.prerelease) for checks, allow_pre in groups)

    candidates = [(parsed, version) for version in versions if (parsed := parse_version(version))]
    matching = [(parsed, version) for parsed, version in candidates if satisfies(parsed)]
    return max(matching, key=lambda pair: pair[0])[1] if matching else None


def discover_roots(checkout: Path, deploy_dir: str) -> list[str]:
    """Every kustomization under deploy_dir that no other kustomization under it references."""
    dirs = {path.parent.resolve() for path in (checkout / deploy_dir).rglob("kustomization.yaml")}
    referenced = set()
    for directory in dirs:
        spec = yaml.safe_load((directory / "kustomization.yaml").read_text()) or {}
        for key in ("resources", "components", "bases"):
            for entry in spec.get(key) or []:
                if isinstance(entry, str) and "://" not in entry:
                    referenced.add((directory / entry).resolve())
    return sorted(str(directory.relative_to(checkout.resolve())) for directory in dirs - referenced)


class ToolError(Exception):
    """An external tool is missing or failed."""


def run_tool(*command: str, cwd: Path | None = None) -> str:
    try:
        return subprocess.run(command, capture_output=True, text=True, check=True, cwd=cwd).stdout
    except FileNotFoundError as error:
        raise ToolError(f"{command[0]} not found on PATH") from error
    except subprocess.CalledProcessError as error:
        raise ToolError(f"{' '.join(command[:2])}: {error.stderr.strip()}") from error


VARIABLE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::?[=-]([^}]*))?\}")


def load_vars(path: Path) -> dict[str, str]:
    """Read KEY=VALUE lines, as in a postBuild substitute ConfigMap."""
    pairs = (line.split("=", 1) for line in path.read_text().splitlines() if "=" in line and not line.startswith("#"))
    return {key.strip(): value.strip().strip("\"'") for key, value in pairs}


def substitute(text: str, variables: dict[str, str] | None) -> str:
    """Replace ${VAR} references from variables, falling back to ${VAR:=default} defaults.

    Without variables, postBuild values are unknown, so every reference stays literal.
    """
    if variables is None:
        return text

    def replace(match: re.Match[str]) -> str:
        if match[1] in variables:
            return variables[match[1]]
        return match[2] if match[2] is not None else match[0]

    return VARIABLE.sub(replace, text)


KUSTOMIZATION_FILES = ("kustomization.yaml", "kustomization.yml", "Kustomization")


def kustomize_build(path: Path, variables: dict[str, str] | None) -> list[Doc]:
    if any((path / name).is_file() for name in KUSTOMIZATION_FILES):
        out = run_tool("kustomize", "build", str(path))
    else:
        # Flux generates a kustomization for plain directories; do the same on a copy.
        with tempfile.TemporaryDirectory(prefix="image-map-") as workdir:
            copy = Path(workdir) / path.name
            shutil.copytree(path, copy)
            run_tool("kustomize", "create", "--autodetect", "--recursive", cwd=copy)
            out = run_tool("kustomize", "build", str(copy))
    return [doc for doc in yaml.safe_load_all(substitute(out, variables)) if doc]


CLUSTER_SCOPED = {
    "Namespace", "Node", "PersistentVolume", "StorageClass", "PriorityClass", "RuntimeClass",
    "ClusterRole", "ClusterRoleBinding", "CustomResourceDefinition", "APIService", "IngressClass",
    "MutatingWebhookConfiguration", "ValidatingWebhookConfiguration", "ValidatingAdmissionPolicy",
    "ValidatingAdmissionPolicyBinding", "CSIDriver", "VolumeSnapshotClass", "ClusterIssuer",
}


def set_namespace(docs: list[Doc], namespace: str) -> list[Doc]:
    """Apply a Flux Kustomization's targetNamespace to every namespaced object."""
    for doc in docs:
        if doc.get("kind") not in CLUSTER_SCOPED:
            doc.setdefault("metadata", {})["namespace"] = namespace
    return docs


SourceKey = tuple[str, str | None, str]


class FetchError(Exception):
    pass


class ChartFetcher(Protocol):
    def fetch(self, release: Doc, sources: dict[SourceKey, Doc]) -> Path:
        """Return a local chart directory or archive for release, or raise FetchError."""
        ...


def deep_merge(base: Doc, override: Doc) -> Doc:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def release_values(release: Doc, docs: list[Doc], warn: Callable[[str], None]) -> Doc:
    """Merge valuesFrom ConfigMaps from the release's root, then inline values, as Flux does."""
    namespace = ref(release)["namespace"]
    values: Doc = {}
    for source in release["spec"].get("valuesFrom") or []:
        where = f"{source['kind']} {namespace}/{source['name']}"
        if source["kind"] != "ConfigMap":
            warn(f"{where}: skipped, only ConfigMap values are read")
            continue
        configmap = next(
            (d for d in docs if d.get("kind") == "ConfigMap" and ref(d)["name"] == source["name"]
             and ref(d)["namespace"] == namespace),
            None,
        )
        key = source.get("valuesKey", "values.yaml")
        data = ((configmap or {}).get("data") or {}).get(key)
        if data is None:
            if not source.get("optional"):
                warn(f"{where}: key {key} not found in rendered output")
            continue
        if target := source.get("targetPath"):
            for part in reversed(target.split(".")):
                data = {part: data}
            values = deep_merge(values, data)
        else:
            values = deep_merge(values, yaml.safe_load(data) or {})
    return deep_merge(values, release["spec"].get("values") or {})


def helm_template(chart: Path, release: Doc, values: Doc, kube_version: str | None) -> tuple[list[Doc], str]:
    meta, spec = release["metadata"], release["spec"]
    namespace = spec.get("targetNamespace") or meta.get("namespace") or "default"
    target = spec.get("targetNamespace")
    name = spec.get("releaseName") or (f"{target}-{meta['name']}" if target else meta["name"])
    with tempfile.NamedTemporaryFile("w", suffix=".yaml") as values_file:
        yaml.safe_dump(values, values_file)
        values_file.flush()
        command = ["helm", "template", name, str(chart), "--namespace", namespace, "--include-crds"]
        chart_spec = (spec.get("chart") or {}).get("spec") or {}
        for values_path in chart_spec.get("valuesFiles") or []:
            command += ["--values", str(chart / values_path)]
        command += ["--values", values_file.name]
        if kube_version:
            command += ["--kube-version", kube_version]
        out = run_tool(*command)
    docs = [doc for doc in yaml.safe_load_all(out) if doc]
    if not (spec.get("test") or {}).get("enable"):
        docs = [doc for doc in docs if not is_test_hook(doc)]
    for renderer in spec.get("postRenderers") or []:
        if kustomize := renderer.get("kustomize"):
            docs = post_render(docs, kustomize)
    return docs, namespace


def post_render(docs: list[Doc], kustomize: Doc) -> list[Doc]:
    """Apply a HelmRelease kustomize post-renderer (images, patches) to rendered manifests."""
    with tempfile.TemporaryDirectory(prefix="image-map-") as workdir:
        Path(workdir, "rendered.yaml").write_text(yaml.safe_dump_all(docs))
        kustomization = {"resources": ["rendered.yaml"], **kustomize}
        Path(workdir, "kustomization.yaml").write_text(yaml.safe_dump(kustomization))
        out = run_tool("kustomize", "build", workdir)
    return [doc for doc in yaml.safe_load_all(out) if doc]


def is_test_hook(doc: Doc) -> bool:
    """Helm test hooks, which Flux only runs when the HelmRelease enables tests."""
    hooks = ((doc.get("metadata") or {}).get("annotations") or {}).get("helm.sh/hook", "")
    return any(hook.strip() in ("test", "test-success") for hook in hooks.split(","))


class DigestError(Exception):
    pass


Platforms = list[dict[str, str]] | None


class DigestResolver(Protocol):
    def resolve(self, image: str) -> tuple[str, Platforms]:
        """Return the digest image points to, plus its Platform manifests if it is an Index, or raise DigestError."""
        ...


INDEX_TYPES = {"application/vnd.oci.image.index.v1+json", "application/vnd.docker.distribution.manifest.list.v2+json"}


class Crane:
    """Looks up digests with crane, using ambient docker credentials."""

    def run(self, *args: str) -> str:
        try:
            return run_tool("crane", *args).strip()
        except ToolError as error:
            raise DigestError(f"{args[-1]}: {error}") from error

    def resolve(self, image: str) -> tuple[str, Platforms]:
        name, _, digest = image.partition("@")
        if digest:
            name = name.rsplit(":", 1)[0] if ":" in name.rsplit("/", 1)[-1] else name
        else:
            digest = self.run("digest", image)
        try:
            manifest = json.loads(self.run("manifest", f"{name}@{digest}"))
        except json.JSONDecodeError as error:
            raise DigestError(f"{image}: unreadable manifest: {error}") from error
        if manifest.get("mediaType") not in INDEX_TYPES:
            return digest, None
        platforms = []
        for entry in manifest.get("manifests") or []:
            platform = entry.get("platform") or {}
            parts = [platform.get("os"), platform.get("architecture"), platform.get("variant")]
            label = "/".join(part for part in parts if part)
            if label and label != "unknown/unknown":  # attestation manifests
                platforms.append({"platform": label, "digest": entry["digest"]})
        return digest, platforms


def print_table(rows: list[Doc], stdout: TextIO) -> None:
    def name(obj: dict[str, Any] | None) -> str:
        return "/".join(str(obj[key]) for key in ("kind", "namespace", "name") if obj[key]) if obj else "-"

    lines = [("IMAGE", "CONTROLLER", "WORKLOAD", "SOURCE", "ROOT")]
    for row in rows:
        chart = row.get("chart") or {}
        image = row["image"] or "@".join(str(part) for part in (chart.get("name"), chart.get("version")) if part)
        lines.append((image, name(row["controller"]), name(row["workload"]), row["source"], row["root"]))
    widths = [max(len(line[i]) for line in lines) for i in range(len(lines[0]))]
    for line in lines:
        print("  ".join(cell.ljust(width) for cell, width in zip(line, widths)).rstrip(), file=stdout)


def main(
    argv: list[str],
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
    charts: ChartFetcher | None = None,
    registry: DigestResolver | None = None,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkout", type=Path, help="repository checkout to scan")
    parser.add_argument("--deploy-dir", default="deploy", help="directory holding the Flux roots (default: deploy)")
    parser.add_argument("--root", action="append", help="Flux root path to render instead of discovering them")
    parser.add_argument("--vars", type=Path, help="KEY=VALUE file of known postBuild substitutions")
    parser.add_argument("--kube-version", help="Kubernetes version passed to helm template")
    parser.add_argument("--resolve-digests", action="store_true", help="look up image digests with crane")
    parser.add_argument("--format", choices=("jsonl", "table"), default="jsonl")
    parser.add_argument("--strict", action="store_true", help="exit 1 on any fallback or warning")
    args = parser.parse_args(argv)

    checkout: Path = args.checkout.resolve()
    origin = git_origin(checkout)
    charts = charts or RemoteCharts(checkout, origin)
    registry = registry or Crane()
    variables = load_vars(args.vars) if args.vars else None
    warnings = 0
    failed = False

    def warn(message: str) -> None:
        nonlocal warnings
        warnings += 1
        print(f"warning: {message}", file=stderr)

    built: dict[str, list[Doc]] = {}
    sources: dict[SourceKey, Doc] = {}
    pending: list[tuple[str, str | None]] = [
        (os.path.normpath(root), None) for root in args.root or discover_roots(checkout, args.deploy_dir)
    ]
    seen = {root for root, _ in pending}
    flux_kustomizations: list[tuple[str, Doc]] = []
    # Build every known root before following Flux Kustomizations, so their sources are all indexed.
    while pending or flux_kustomizations:
        if not pending:
            root, flux = flux_kustomizations.pop(0)
            spec = flux.get("spec") or {}
            where = f"{root}: Kustomization {ref(flux)['namespace']}/{ref(flux)['name']}"
            source_ref = spec.get("sourceRef") or {}
            source_key = (
                source_ref.get("kind", "GitRepository"),
                source_ref.get("namespace", ref(flux)["namespace"]),
                str(source_ref.get("name")),
            )
            path = os.path.normpath(spec.get("path") or ".")
            if (source := sources.get(source_key)) is not None and (
                source.get("kind") != "GitRepository" or git_url_key(source["spec"]["url"]) != origin
            ):
                warn(f"{where}: source {source_key[1]}/{source_key[2]} is not this repository, skipped")
            elif not (checkout / path).is_dir():
                warn(f"{where}: path {path} not in checkout")
            elif path not in seen:
                seen.add(path)
                pending.append((path, spec.get("targetNamespace")))
            continue
        root, target_namespace = pending.pop(0)
        try:
            docs = kustomize_build(checkout / root, variables)
        except ToolError as error:
            failed = True
            print(f"error: {root}: kustomize build failed: {error}", file=stderr)
            continue
        built[root] = set_namespace(docs, target_namespace) if target_namespace else docs
        for doc in docs:
            api_version = str(doc.get("apiVersion", ""))
            if api_version.startswith("source.toolkit.fluxcd.io/"):
                sources[(doc["kind"], ref(doc)["namespace"], str(ref(doc)["name"]))] = doc
            elif api_version.startswith("kustomize.toolkit.fluxcd.io/") and doc.get("kind") == "Kustomization":
                flux_kustomizations.append((root, doc))

    rows: list[Doc] = []

    def add(image: str | None, root: str, controller: Doc, workload: dict[str, Any] | None, source: str) -> Doc:
        unresolved = image is not None and "${" in image
        row = {
            "image": image if image is None or unresolved else normalize_image(image),
            "root": root,
            "controller": ref(controller),
            "workload": workload,
            "source": source,
            "unresolved": unresolved,
        }
        rows.append(row)
        return row

    for root, docs in built.items():
        for workload, image, kind in extract_images(docs):
            add(image, root, workload, ref(workload), "kustomize" if kind == "pod" else kind)
        for release in (d for d in docs if d.get("kind") == "HelmRelease"):
            where = f"{root}: HelmRelease {ref(release)['namespace']}/{ref(release)['name']}"
            try:
                chart = charts.fetch(release, sources)
                values = release_values(release, docs, lambda message: warn(f"{where}: {message}"))
                rendered, namespace = helm_template(chart, release, values, args.kube_version)
            except (FetchError, ToolError) as error:
                warn(f"{where}: not rendered: {error}")
                row = add(None, root, release, None, "helm-unrendered")
                row["chart"] = release_chart(release)
                continue
            for workload, image, kind in extract_images(rendered):
                workload_ref = ref(workload)
                workload_ref["namespace"] = workload_ref["namespace"] or namespace
                add(image, root, release, workload_ref, "helm" if kind == "pod" else kind)

    unique = list({json.dumps(row, sort_keys=True): row for row in rows}.values())
    if args.resolve_digests:
        lookups: dict[str, tuple[str | None, Platforms]] = {}
        for row in unique:
            image = row["image"]
            if image is not None and not row["unresolved"] and image not in lookups:
                try:
                    lookups[image] = registry.resolve(image)
                except DigestError as error:
                    warn(f"digest lookup failed: {error}")
                    lookups[image] = (None, None)
            row["digest"], row["platforms"] = lookups.get(image or "", (None, None))
    if args.format == "table":
        print_table(unique, stdout)
    else:
        for row in unique:
            print(json.dumps(row), file=stdout)
    if failed:
        return 1
    degraded = warnings or any(row["unresolved"] for row in unique)
    return 1 if args.strict and degraded else 0


def git_url_key(url: str) -> str:
    """Normalize a git URL to host/path, so ssh and https remotes of one repo compare equal."""
    url = re.sub(r"^[a-z+]+://", "", url.strip())
    url = re.sub(r"^[^@/]+@", "", url).replace(":", "/", 1)
    return url.removesuffix("/").removesuffix(".git").lower()


def git_origin(checkout: Path) -> str | None:
    try:
        return git_url_key(run_tool("git", "-C", str(checkout), "remote", "get-url", "origin"))
    except ToolError:
        return None


def release_chart(release: Doc) -> dict[str, str | None]:
    """What a HelmRelease deploys, for rows whose chart could not be rendered."""
    spec = release.get("spec") or {}
    if chart_ref := spec.get("chartRef"):
        return {"name": f"{chart_ref.get('kind')}/{chart_ref.get('name')}", "version": None}
    chart = (spec.get("chart") or {}).get("spec") or {}
    return {"name": chart.get("chart"), "version": chart.get("version")}


class RemoteCharts:
    """Fetches charts from Flux sources using ambient helm, docker and git credentials."""

    def __init__(self, checkout: Path, origin: str | None) -> None:
        self.checkout = checkout
        self.origin = origin
        self.workdir = tempfile.TemporaryDirectory(prefix="image-map-")
        self.cache: dict[tuple[str, ...], Path] = {}

    def run(self, *command: str) -> str:
        try:
            return run_tool(*command)
        except ToolError as error:
            raise FetchError(str(error)) from error

    def source(self, sources: dict[SourceKey, Doc], kind: str, namespace: str | None, name: str) -> Doc:
        if (found := sources.get((kind, namespace, name))) is None:
            raise FetchError(f"{kind} {namespace}/{name} not found in rendered roots")
        return found

    def fetch(self, release: Doc, sources: dict[SourceKey, Doc]) -> Path:
        try:
            return self.fetch_chart(release, sources)
        except (KeyError, TypeError) as error:
            raise FetchError(f"malformed chart reference: {error!r}") from error

    def fetch_chart(self, release: Doc, sources: dict[SourceKey, Doc]) -> Path:
        namespace = ref(release)["namespace"]
        spec = release["spec"]
        if chart_ref := spec.get("chartRef"):
            source = self.source(sources, chart_ref["kind"], chart_ref.get("namespace", namespace), chart_ref["name"])
            if chart_ref["kind"] == "OCIRepository":
                return self.oci_repository(source)
            chart = source["spec"]  # HelmChart
            namespace = ref(source)["namespace"]
        else:
            chart = (spec.get("chart") or {}).get("spec") or {}
        source_ref = chart["sourceRef"]
        source = self.source(sources, source_ref["kind"], source_ref.get("namespace", namespace), source_ref["name"])
        match source_ref["kind"]:
            case "HelmRepository":
                return self.helm_repository(source, chart["chart"], chart.get("version") or "*")
            case "GitRepository":
                return self.git_repository(source, chart["chart"])
        raise FetchError(f"{source_ref['kind']} sources are not supported")

    def pull(self, key: tuple[str, ...], *args: str) -> Path:
        if key not in self.cache:
            target = Path(self.workdir.name) / str(len(self.cache))
            self.run("helm", "pull", *args, "--untar", "--untardir", str(target))
            self.cache[key] = next(target.iterdir())
        return self.cache[key]

    def helm_repository(self, source: Doc, chart: str, version: str) -> Path:
        url = source["spec"]["url"].rstrip("/")
        if source["spec"].get("type") != "oci":
            return self.pull((url, chart, version), chart, "--repo", url, "--version", version)
        repository = f"{url}/{chart}"
        tags = self.run("crane", "ls", repository.removeprefix("oci://")).split()
        if (resolved := resolve_version(version, tags)) is None:
            raise FetchError(f"no version of {repository} matches {version}")
        return self.pull((repository, resolved), repository, "--version", resolved)

    def oci_repository(self, source: Doc) -> Path:
        url, oci_ref = source["spec"]["url"], source["spec"].get("ref") or {}
        if digest := oci_ref.get("digest"):
            return self.pull((url, digest), f"{url}@{digest}")
        if constraint := oci_ref.get("semver"):
            tag = resolve_version(constraint, self.run("crane", "ls", url.removeprefix("oci://")).split())
            if tag is None:
                raise FetchError(f"no version of {url} matches {constraint}")
        else:
            tag = oci_ref.get("tag", "latest")
        return self.pull((url, tag), url, "--version", tag)

    def git_repository(self, source: Doc, chart: str) -> Path:
        url = source["spec"]["url"]
        if self.origin is not None and git_url_key(url) == self.origin:
            root = self.checkout
        else:
            git_ref = source["spec"].get("ref") or {}
            branch = git_ref.get("tag") or git_ref.get("branch")
            key = (url, branch or "")
            if key not in self.cache:
                target = Path(self.workdir.name) / str(len(self.cache))
                self.run("git", "clone", "--depth", "1", *(["--branch", branch] if branch else []), url, str(target))
                self.cache[key] = target
            root = self.cache[key]
        path = root / chart
        if not path.is_dir():
            raise FetchError(f"chart path {chart} not found in {url}")
        if (yaml.safe_load((path / "Chart.yaml").read_text()) or {}).get("dependencies"):
            deps_key = (url, str(root), chart, "deps")
            if deps_key not in self.cache:
                # Build dependencies on a copy, so the checkout is left untouched.
                copy = Path(self.workdir.name) / f"deps-{len(self.cache)}" / path.name
                shutil.copytree(path, copy)
                self.run("helm", "dependency", "build", str(copy))
                self.cache[deps_key] = copy
            path = self.cache[deps_key]
        return path


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
