# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml", "semver"]
# ///
"""Map every container image deployed from Flux Kustomization roots to its controller."""

import argparse
import hashlib
import itertools
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, TextIO

import semver
import yaml

Doc = dict[str, Any]


def dig(node: Any, *keys: str) -> Any:
    """Walk nested mappings, returning None as soon as a key is missing."""
    for key in keys:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


@dataclass(frozen=True)
class ObjectRef:
    kind: str
    namespace: str | None
    name: str

    @classmethod
    def of(cls, doc: Doc) -> "ObjectRef":
        return cls(str(doc.get("kind")), dig(doc, "metadata", "namespace"), str(dig(doc, "metadata", "name")))

    def __str__(self) -> str:
        return f"{self.kind} {self.namespace}/{self.name}"

    def label(self) -> str:
        return "/".join(part for part in (self.kind, self.namespace, self.name) if part)

    def to_json(self) -> dict[str, str | None]:
        return {"kind": self.kind, "namespace": self.namespace, "name": self.name}


# Image references


def split_image_ref(image: str) -> tuple[str, str | None, str | None]:
    """Split an image reference into repository, tag and digest."""
    name, _, digest = image.partition("@")
    repository, tag = name, None
    if ":" in name.rsplit("/", 1)[-1]:
        repository, tag = name.rsplit(":", 1)
    return repository, tag, digest or None


def normalize_image(image: str) -> str:
    repository, tag, digest = split_image_ref(image)
    first, _, rest = repository.partition("/")
    if not rest or not ("." in first or ":" in first or first == "localhost"):
        repository = f"docker.io/{repository}" if rest else f"docker.io/library/{repository}"
    elif first == "docker.io" and "/" not in rest:
        repository = f"docker.io/library/{rest}"
    if tag is None and digest is None:
        tag = "latest"
    return repository + (f":{tag}" if tag else "") + (f"@{digest}" if digest else "")


# Image extraction


POD_SPEC_PATHS = {
    "Pod": ("spec",),
    **dict.fromkeys(("Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "Job"), ("spec", "template", "spec")),
    "CronJob": ("spec", "jobTemplate", "spec", "template", "spec"),
}


def pod_spec(doc: Doc) -> Doc | None:
    path = POD_SPEC_PATHS.get(str(doc.get("kind")))
    spec = dig(doc, *path) if path else None
    return spec if isinstance(spec, dict) else None


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


def extract_images(docs: list[Doc], pod_source: str) -> list[tuple[Doc, str, str]]:
    """Return (workload, image reference, source) found in rendered manifests.

    Pod spec images get pod_source; image keys found anywhere in other kinds get "generic".
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
                    found.append((doc, container["image"], pod_source))
    return found


# Semver constraints, as Flux evaluates them (Masterminds semver)

COMPARATOR = re.compile(
    r"(!=|>=|<=|>|<|~>|~|\^|=)?\s*v?(\*|[xX]|\d+)(?:\.(\*|[xX]|\d+))?(?:\.(\*|[xX]|\d+))?"
    r"(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?"
)
HYPHEN_RANGE = re.compile(r"(\S+)\s+-\s+(\S+)")


def parse_version(version: str) -> semver.Version | None:
    try:
        return semver.Version.parse(version.removeprefix("v"))
    except ValueError:
        return None


def comparator(op: str, parts: list[str | None], pre: str | None) -> Callable[[semver.Version], bool]:
    numbers = [int(part) for part in parts if part is not None and part not in "*xX"]
    if not numbers:
        return lambda v: op not in ("!=", ">", "<")
    major, minor, patch = (numbers + [0, 0])[:3]
    lower = semver.Version(major, minor, patch, prerelease=pre)
    release = semver.Version(major, minor, patch)
    # Exclusive upper bound of the versions a partial version like 1.2 stands for.
    upper = (release.bump_major, release.bump_minor, release.bump_patch)[len(numbers) - 1]()
    if op in ("~", "~>"):
        upper = release.bump_major() if len(numbers) == 1 else release.bump_minor()
    if op == "^":
        index = next((i for i, n in enumerate(numbers) if n != 0), len(numbers) - 1)
        upper = (release.bump_major, release.bump_minor, release.bump_patch)[index]()
    exact = len(numbers) == 3
    match op:
        case "" | "=" | "~" | "~>" | "^":
            return (lambda v: v == lower) if exact and op in ("", "=") else (lambda v: lower <= v < upper)
        case "!=":
            return (lambda v: v != lower) if exact else (lambda v: not lower <= v < upper)
        case ">":
            return (lambda v: v > lower) if exact else (lambda v: v >= upper)
        case ">=":
            return lambda v: v >= lower
        case "<":
            return lambda v: v < lower
        case "<=":
            return (lambda v: v <= lower) if exact else (lambda v: v < upper)
    raise ValueError(op)


def resolve_version(constraint: str, versions: Iterable[str]) -> str | None:
    """The highest of versions satisfying constraint, or None."""
    groups = []
    for group in (constraint.strip() or "*").split("||"):
        matches = list(COMPARATOR.finditer(HYPHEN_RANGE.sub(r">=\1 <=\2", group)))
        checks = [comparator(m[1] or "", [m[2], m[3], m[4]], m[5]) for m in matches]
        groups.append((checks, any(m[5] for m in matches)))

    def satisfies(v: semver.Version) -> bool:
        return any(all(check(v) for check in checks) and (allow_pre or not v.prerelease) for checks, allow_pre in groups)

    candidates = [(parsed, version) for version in versions if (parsed := parse_version(version))]
    matching = [(parsed, version) for parsed, version in candidates if satisfies(parsed)]
    return max(matching, key=lambda pair: pair[0])[1] if matching else None


# External tools


class ToolError(Exception):
    """An external tool is missing or failed."""


def run_tool(*command: str, cwd: Path | None = None) -> str:
    try:
        return subprocess.run(command, capture_output=True, text=True, check=True, cwd=cwd).stdout
    except FileNotFoundError as error:
        raise ToolError(f"{command[0]} not found on PATH") from error
    except subprocess.CalledProcessError as error:
        raise ToolError(f"{' '.join(command[:2])}: {error.stderr.strip()}") from error


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


# Rendering Flux roots

KUSTOMIZATION_FILES = ("kustomization.yaml", "kustomization.yml", "Kustomization")
# Unescaped ${VAR}, ${VAR:=default} or ${VAR:-default}; $${VAR} is Flux's escape for a literal ${VAR}.
VARIABLE = re.compile(r"(?<!\$)\$\{([A-Za-z_][A-Za-z0-9_]*)(?::?[=-]([^}]*))?\}")


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


def load_vars(path: Path) -> dict[str, str]:
    """Read KEY=VALUE lines, as in a postBuild substitute ConfigMap."""
    pairs = (line.split("=", 1) for line in path.read_text().splitlines() if "=" in line and not line.startswith("#"))
    return {key.strip(): value.strip().strip("\"'") for key, value in pairs}


def substitute(text: str, variables: dict[str, str] | None) -> str:
    """Replace ${VAR} references from variables, falling back to ${VAR:=default} defaults.

    Without variables, postBuild values are unknown, so every reference stays literal.
    Escaped $${VAR} references are left for unescape_image.
    """
    if variables is None:
        return text

    def replace(match: re.Match[str]) -> str:
        if match[1] in variables:
            return variables[match[1]]
        return match[2] if match[2] is not None else match[0]

    return VARIABLE.sub(replace, text)


def unescape_image(image: str) -> str:
    return image.replace("$${", "${")


def kustomize_build(path: Path, variables: dict[str, str] | None) -> list[Doc]:
    if any((path / name).is_file() for name in KUSTOMIZATION_FILES):
        out = run_tool("kustomize", "build", str(path))
    else:
        # Flux generates a kustomization for plain directories; do the same on a copy.
        # Point its resources back at the originals, so their relative references still resolve.
        with tempfile.TemporaryDirectory(prefix="image-map-") as workdir:
            copy = Path(workdir) / path.name
            shutil.copytree(path, copy)
            run_tool("kustomize", "create", "--autodetect", "--recursive", cwd=copy)
            kustomization = yaml.safe_load((copy / "kustomization.yaml").read_text())
            kustomization["resources"] = [
                os.path.relpath(path.resolve() / resource, copy) for resource in kustomization.get("resources") or []
            ]
            (copy / "kustomization.yaml").write_text(yaml.safe_dump(kustomization))
            out = run_tool("kustomize", "build", "--load-restrictor", "LoadRestrictionsNone", str(copy))
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


def is_flux(doc: Doc, group: str, kind: str | None = None) -> bool:
    return str(doc.get("apiVersion", "")).startswith(f"{group}.toolkit.fluxcd.io/") and kind in (None, doc.get("kind"))


class Log:
    def __init__(self, stderr: TextIO) -> None:
        self.stderr = stderr
        self.warnings = 0
        self.failed = False

    def warning(self, message: str) -> None:
        self.warnings += 1
        print(f"warning: {message}", file=self.stderr)

    def scoped(self, context: str) -> Callable[[str], None]:
        """A warning callback that prefixes each message with context."""
        return lambda message: self.warning(f"{context}: {message}")

    def error(self, message: str) -> None:
        self.failed = True
        print(f"error: {message}", file=self.stderr)


Sources = dict[ObjectRef, Doc]


def render_roots(
    checkout: Path, roots: list[str], variables: dict[str, str] | None, origin: str | None, log: Log
) -> tuple[dict[str, list[Doc]], Sources]:
    """Build each root, then follow the local paths of the Flux Kustomizations they render."""
    rendered: dict[str, list[Doc]] = {}
    sources: Sources = {}
    pending: list[tuple[str, str | None]] = [(os.path.normpath(root), None) for root in roots]
    seen = {root for root, _ in pending}
    flux_kustomizations: list[tuple[str, Doc]] = []

    def follow(root: str, flux_kustomization: Doc) -> None:
        context = f"{root}: {ObjectRef.of(flux_kustomization)}"
        spec = flux_kustomization.get("spec") or {}
        source_ref = spec.get("sourceRef") or {}
        source_key = ObjectRef(
            source_ref.get("kind", "GitRepository"),
            source_ref.get("namespace", dig(flux_kustomization, "metadata", "namespace")),
            str(source_ref.get("name")),
        )
        path = os.path.normpath(spec.get("path") or ".")
        # A source not rendered here is the one azurerm_kubernetes_flux_configuration created: this repository.
        source = sources.get(source_key)
        if source is not None and (source["kind"] != "GitRepository" or git_url_key(source["spec"]["url"]) != origin):
            log.warning(f"{context}: source {source_key.namespace}/{source_key.name} is not this repository, skipped")
        elif not (checkout / path).is_dir():
            log.warning(f"{context}: path {path} not in checkout")
        elif path not in seen:
            seen.add(path)
            pending.append((path, spec.get("targetNamespace")))

    # Build every known root before following Flux Kustomizations, so their sources are all indexed.
    while pending or flux_kustomizations:
        if not pending:
            follow(*flux_kustomizations.pop(0))
            continue
        root, target_namespace = pending.pop(0)
        try:
            docs = kustomize_build(checkout / root, variables)
        except ToolError as error:
            log.error(f"{root}: kustomize build failed: {error}")
            continue
        rendered[root] = set_namespace(docs, target_namespace) if target_namespace else docs
        for doc in docs:
            if is_flux(doc, "source"):
                sources[ObjectRef.of(doc)] = doc
            elif is_flux(doc, "kustomize", "Kustomization"):
                flux_kustomizations.append((root, doc))
    return rendered, sources


# HelmReleases


@dataclass(frozen=True)
class Chart:
    name: str | None
    version: str | None

    def label(self) -> str:
        return "@".join(part for part in (self.name, self.version) if part)


def shorten_release_name(name: str) -> str:
    """Flux's release name shortening, for names over Helm's 53 character limit."""
    if len(name) <= 53:
        return name
    return f"{name[:40]}-{hashlib.sha256(name.encode()).hexdigest()[:12]}"


class HelmRelease:
    def __init__(self, doc: Doc) -> None:
        self.doc = doc
        self.ref = ObjectRef.of(doc)
        self.spec: Doc = doc.get("spec") or {}
        self.chart_spec: Doc = dig(self.spec, "chart", "spec") or {}

    @property
    def target_namespace(self) -> str:
        return self.spec.get("targetNamespace") or self.ref.namespace or "default"

    @property
    def release_name(self) -> str:
        target = self.spec.get("targetNamespace")
        return shorten_release_name(self.spec.get("releaseName") or (f"{target}-{self.ref.name}" if target else self.ref.name))

    @property
    def chart(self) -> Chart:
        if chart_ref := self.spec.get("chartRef"):
            return Chart(f"{chart_ref.get('kind')}/{chart_ref.get('name')}", None)
        return Chart(self.chart_spec.get("chart"), self.chart_spec.get("version"))

    def values(self, docs: list[Doc], warn: Callable[[str], None]) -> Doc:
        """Merge valuesFrom ConfigMaps from the release's root, then inline values, as Flux does."""
        values: Doc = {}
        for source in self.spec.get("valuesFrom") or []:
            source_ref = ObjectRef(source["kind"], self.ref.namespace, source["name"])
            if source_ref.kind != "ConfigMap":
                warn(f"{source_ref}: skipped, only ConfigMap values are read")
                continue
            configmap = next((doc for doc in docs if ObjectRef.of(doc) == source_ref), None)
            key = source.get("valuesKey", "values.yaml")
            data = dig(configmap, "data", key)
            if data is None:
                if not source.get("optional"):
                    warn(f"{source_ref}: key {key} not found in rendered output")
                continue
            if target := source.get("targetPath"):
                for part in reversed(target.split(".")):
                    data = {part: data}
                values = deep_merge(values, data)
            else:
                values = deep_merge(values, yaml.safe_load(data) or {})
        return deep_merge(values, self.spec.get("values") or {})

    def template(self, chart: Path, values: Doc, kube_version: str | None) -> list[Doc]:
        """Render the chart as helm-controller would install it."""
        with tempfile.NamedTemporaryFile("w", suffix=".yaml") as values_file:
            yaml.safe_dump(values, values_file)
            values_file.flush()
            command = ["helm", "template", self.release_name, str(chart)]
            command += ["--namespace", self.target_namespace, "--include-crds"]
            for values_path in self.chart_spec.get("valuesFiles") or []:
                command += ["--values", str(chart / values_path)]
            command += ["--values", values_file.name]
            if kube_version:
                command += ["--kube-version", kube_version]
            docs = [doc for doc in yaml.safe_load_all(run_tool(*command)) if doc]
        if not dig(self.spec, "test", "enable"):
            docs = [doc for doc in docs if not is_test_hook(doc)]
        for renderer in self.spec.get("postRenderers") or []:
            if kustomize := renderer.get("kustomize"):
                docs = post_render(docs, kustomize)
        for doc in docs:
            if doc.get("kind") not in CLUSTER_SCOPED:
                doc.setdefault("metadata", {}).setdefault("namespace", self.target_namespace)
        return docs


def deep_merge(base: Doc, override: Doc) -> Doc:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def is_test_hook(doc: Doc) -> bool:
    """Helm test hooks, which Flux only runs when the HelmRelease enables tests."""
    hooks = dig(doc, "metadata", "annotations", "helm.sh/hook") or ""
    return any(hook.strip() in ("test", "test-success") for hook in hooks.split(","))


def post_render(docs: list[Doc], kustomize: Doc) -> list[Doc]:
    """Apply a HelmRelease kustomize post-renderer (images, patches) to rendered manifests."""
    with tempfile.TemporaryDirectory(prefix="image-map-") as workdir:
        Path(workdir, "rendered.yaml").write_text(yaml.safe_dump_all(docs))
        kustomization = {"resources": ["rendered.yaml"], **kustomize}
        Path(workdir, "kustomization.yaml").write_text(yaml.safe_dump(kustomization))
        return [doc for doc in yaml.safe_load_all(run_tool("kustomize", "build", workdir)) if doc]


# Chart fetching


class FetchError(Exception):
    pass


class ChartFetcher(Protocol):
    def fetch(self, release: HelmRelease, sources: Sources) -> Path:
        """Return a local chart directory for release, or raise FetchError."""
        ...


class RemoteCharts:
    """Fetches charts from Flux sources using ambient helm, docker and git credentials."""

    def __init__(self, checkout: Path, origin: str | None) -> None:
        self.checkout = checkout
        self.origin = origin
        self.workdir = tempfile.TemporaryDirectory(prefix="image-map-")
        self.dir_numbers = itertools.count()
        self.cache: dict[tuple[str, ...], Path] = {}

    def run(self, *command: str, cwd: Path | None = None) -> str:
        try:
            return run_tool(*command, cwd=cwd)
        except ToolError as error:
            raise FetchError(str(error)) from error

    def cached(self, key: tuple[str, ...], make: Callable[[Path], Path]) -> Path:
        """Return the cached path for key, else make it in a fresh scratch directory."""
        if key not in self.cache:
            self.cache[key] = make(Path(self.workdir.name) / str(next(self.dir_numbers)))
        return self.cache[key]

    def source(self, sources: Sources, source_ref: ObjectRef) -> Doc:
        if (found := sources.get(source_ref)) is None:
            raise FetchError(f"{source_ref} not found in rendered roots")
        return found

    def fetch(self, release: HelmRelease, sources: Sources) -> Path:
        try:
            return self.fetch_chart(release, sources)
        except (KeyError, TypeError) as error:
            raise FetchError(f"malformed chart reference: {error!r}") from error

    def fetch_chart(self, release: HelmRelease, sources: Sources) -> Path:
        namespace = release.ref.namespace
        chart = release.chart_spec
        if chart_ref := release.spec.get("chartRef"):
            source = self.source(sources, ObjectRef(chart_ref["kind"], chart_ref.get("namespace", namespace), chart_ref["name"]))
            if chart_ref["kind"] == "OCIRepository":
                return self.oci_repository(source)
            chart, namespace = source["spec"], ObjectRef.of(source).namespace  # HelmChart
        source_ref = ObjectRef(
            chart["sourceRef"]["kind"], chart["sourceRef"].get("namespace", namespace), chart["sourceRef"]["name"]
        )
        source = self.source(sources, source_ref)
        match source_ref.kind:
            case "HelmRepository":
                return self.helm_repository(source, chart["chart"], chart.get("version") or "*")
            case "GitRepository":
                return self.git_repository(source, chart["chart"])
        raise FetchError(f"{source_ref.kind} sources are not supported")

    def pull(self, *args: str) -> Path:
        def make(target: Path) -> Path:
            self.run("helm", "pull", *args, "--untar", "--untardir", str(target))
            return next(target.iterdir())

        return self.cached(("helm", *args), make)

    def oci_tag(self, repository: str, constraint: str) -> str:
        tags = self.run("crane", "ls", repository.removeprefix("oci://")).split()
        if (tag := resolve_version(constraint, tags)) is None:
            raise FetchError(f"no version of {repository} matches {constraint}")
        return tag

    def helm_repository(self, source: Doc, chart: str, version: str) -> Path:
        url = source["spec"]["url"].rstrip("/")
        if source["spec"].get("type") != "oci":
            return self.pull(chart, "--repo", url, "--version", version)
        repository = f"{url}/{chart}"
        return self.pull(repository, "--version", self.oci_tag(repository, version))

    def oci_repository(self, source: Doc) -> Path:
        url, oci_ref = source["spec"]["url"], source["spec"].get("ref") or {}
        if digest := oci_ref.get("digest"):
            return self.pull(f"{url}@{digest}")
        tag = self.oci_tag(url, oci_ref["semver"]) if oci_ref.get("semver") else oci_ref.get("tag", "latest")
        return self.pull(url, "--version", tag)

    def git_fetch_ref(self, url: str, git_ref: Doc) -> str:
        """The ref Flux checks out, by its precedence: commit, name, semver, tag, branch (default master)."""
        if commit := git_ref.get("commit"):
            return str(commit)
        if name := git_ref.get("name"):
            return str(name)
        if constraint := git_ref.get("semver"):
            listing = self.run("git", "ls-remote", "--tags", url).splitlines()
            tags = {line.split("refs/tags/", 1)[1].removesuffix("^{}") for line in listing if "refs/tags/" in line}
            if (tag := resolve_version(constraint, tags)) is None:
                raise FetchError(f"no tag of {url} matches {constraint}")
            return f"refs/tags/{tag}"
        if tag := git_ref.get("tag"):
            return f"refs/tags/{tag}"
        return f"refs/heads/{git_ref.get('branch') or 'master'}"

    def git_clone(self, url: str, git_ref: Doc) -> Path:
        fetch_ref = self.git_fetch_ref(url, git_ref)

        def make(target: Path) -> Path:
            self.run("git", "init", "-q", str(target))
            try:
                self.run("git", "fetch", "-q", "--depth", "1", url, fetch_ref, cwd=target)
                revision = "FETCH_HEAD"
            except FetchError:
                if not git_ref.get("commit"):
                    raise
                # Servers may refuse fetching a commit by id; fetch the branches and find it.
                self.run("git", "fetch", "-q", url, "+refs/heads/*:refs/remotes/origin/*", cwd=target)
                revision = fetch_ref
            self.run("git", "checkout", "-q", revision, cwd=target)
            return target

        return self.cached(("git", url, fetch_ref), make)

    def git_repository(self, source: Doc, chart: str) -> Path:
        url = source["spec"]["url"]
        same_repo = self.origin is not None and git_url_key(url) == self.origin
        root = self.checkout if same_repo else self.git_clone(url, source["spec"].get("ref") or {})
        path = root / chart
        if not path.is_dir():
            raise FetchError(f"chart path {chart} not found in {url}")
        if not (yaml.safe_load((path / "Chart.yaml").read_text()) or {}).get("dependencies"):
            return path

        def build_dependencies(target: Path) -> Path:
            # Build dependencies on a copy, so the checkout is left untouched.
            copy = target / path.name
            shutil.copytree(path, copy)
            self.run("helm", "dependency", "build", str(copy))
            return copy

        return self.cached(("deps", str(path)), build_dependencies)


# Rows


@dataclass(frozen=True)
class Row:
    image: str | None
    root: str
    controller: ObjectRef
    workload: ObjectRef | None
    source: str
    unresolved: bool = False
    chart: Chart | None = None

    @classmethod
    def for_image(cls, image: str, root: str, controller: ObjectRef, workload: ObjectRef, source: str) -> "Row":
        unresolved = VARIABLE.search(image) is not None
        image = unescape_image(image)
        return cls(image if unresolved else normalize_image(image), root, controller, workload, source, unresolved)

    def to_json(self) -> Doc:
        row: Doc = {
            "image": self.image,
            "root": self.root,
            "controller": self.controller.to_json(),
            "workload": self.workload and self.workload.to_json(),
            "source": self.source,
            "unresolved": self.unresolved,
        }
        if self.chart:
            row["chart"] = {"name": self.chart.name, "version": self.chart.version}
        return row


def map_images(
    rendered: dict[str, list[Doc]], sources: Sources, charts: ChartFetcher, kube_version: str | None, log: Log
) -> list[Row]:
    """One row per image and controller, in root order, duplicates collapsed."""
    rows: list[Row] = []
    for root, docs in rendered.items():
        for workload, image, source in extract_images(docs, "kustomize"):
            rows.append(Row.for_image(image, root, ObjectRef.of(workload), ObjectRef.of(workload), source))
        for release in (HelmRelease(doc) for doc in docs if doc.get("kind") == "HelmRelease"):
            context = f"{root}: {release.ref}"
            try:
                chart = charts.fetch(release, sources)
                values = release.values(docs, log.scoped(context))
                templated = release.template(chart, values, kube_version)
            except (FetchError, ToolError) as error:
                log.warning(f"{context}: not rendered: {error}")
                rows.append(Row(None, root, release.ref, None, "helm-unrendered", chart=release.chart))
                continue
            for workload, image, source in extract_images(templated, "helm"):
                rows.append(Row.for_image(image, root, release.ref, ObjectRef.of(workload), source))
    return list(dict.fromkeys(rows))


# Digests

PlatformManifests = list[dict[str, str]] | None
Digests = dict[str, tuple[str | None, PlatformManifests]]


class DigestError(Exception):
    pass


class DigestResolver(Protocol):
    def resolve(self, image: str) -> tuple[str, PlatformManifests]:
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

    def resolve(self, image: str) -> tuple[str, PlatformManifests]:
        repository, _, digest = split_image_ref(image)
        digest = digest or self.run("digest", image)
        try:
            manifest = json.loads(self.run("manifest", f"{repository}@{digest}"))
        except json.JSONDecodeError as error:
            raise DigestError(f"{image}: unreadable manifest: {error}") from error
        if manifest.get("mediaType") not in INDEX_TYPES:
            return digest, None
        platforms = []
        for entry in manifest.get("manifests") or []:
            platform = entry.get("platform") or {}
            label = "/".join(filter(None, (platform.get("os"), platform.get("architecture"), platform.get("variant"))))
            if label and label != "unknown/unknown":  # attestation manifests
                platforms.append({"platform": label, "digest": entry["digest"]})
        return digest, platforms


def resolve_digests(rows: list[Row], registry: DigestResolver, log: Log) -> Digests:
    """Digest and Platform manifests per resolvable image reference; failed lookups map to (None, None)."""
    lookups: Digests = {}
    for row in rows:
        if row.image is None or row.unresolved or row.image in lookups:
            continue
        try:
            lookups[row.image] = registry.resolve(row.image)
        except DigestError as error:
            log.warning(f"digest lookup failed: {error}")
            lookups[row.image] = (None, None)
    return lookups


# Output


def lookup(digests: Digests, row: Row) -> tuple[str | None, PlatformManifests]:
    return digests.get(row.image or "", (None, None))


def print_table(rows: list[Row], digests: Digests | None, stdout: TextIO) -> None:
    header = ["IMAGE", "CONTROLLER", "WORKLOAD", "SOURCE", "ROOT"]
    if digests is not None:
        header.insert(1, "DIGEST")
    lines = [header]
    for row in rows:
        image = row.image or (row.chart.label() if row.chart else "-")
        workload = row.workload.label() if row.workload else "-"
        line = [image, row.controller.label(), workload, row.source, row.root]
        if digests is not None:
            line.insert(1, lookup(digests, row)[0] or "-")
        lines.append(line)
    widths = [max(len(line[i]) for line in lines) for i in range(len(header))]
    for line in lines:
        print("  ".join(cell.ljust(width) for cell, width in zip(line, widths)).rstrip(), file=stdout)


def print_jsonl(rows: list[Row], digests: Digests | None, stdout: TextIO) -> None:
    for row in rows:
        output = row.to_json()
        if digests is not None:
            output["digest"], output["platforms"] = lookup(digests, row)
        print(json.dumps(output), file=stdout)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkout", type=Path, help="repository checkout to scan")
    parser.add_argument("--deploy-dir", default="deploy", help="directory holding the Flux roots (default: deploy)")
    parser.add_argument("--root", action="append", help="Flux root path to render instead of discovering them")
    parser.add_argument("--vars", type=Path, help="KEY=VALUE file of known postBuild substitutions")
    parser.add_argument("--kube-version", help="Kubernetes version passed to helm template")
    parser.add_argument("--resolve-digests", action="store_true", help="look up image digests with crane")
    parser.add_argument("--format", choices=("jsonl", "table"), default="jsonl")
    parser.add_argument("--strict", action="store_true", help="exit 1 on any fallback or warning")
    return parser.parse_args(argv)


def main(
    argv: list[str],
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
    charts: ChartFetcher | None = None,
    registry: DigestResolver | None = None,
) -> int:
    args = parse_args(argv)
    checkout: Path = args.checkout.resolve()
    origin = git_origin(checkout)
    log = Log(stderr)

    roots = args.root or discover_roots(checkout, args.deploy_dir)
    variables = load_vars(args.vars) if args.vars else None
    rendered, sources = render_roots(checkout, roots, variables, origin, log)
    rows = map_images(rendered, sources, charts or RemoteCharts(checkout, origin), args.kube_version, log)
    digests = resolve_digests(rows, registry or Crane(), log) if args.resolve_digests else None

    if args.format == "table":
        print_table(rows, digests, stdout)
    else:
        print_jsonl(rows, digests, stdout)

    if log.failed:
        return 1
    degraded = log.warnings or any(row.unresolved for row in rows)
    return 1 if args.strict and degraded else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
