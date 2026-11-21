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


VARIABLE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::?[=-][^}]*)?\}")


def load_vars(path: Path) -> dict[str, str]:
    """Read KEY=VALUE lines, as in a postBuild substitute ConfigMap."""
    pairs = (line.split("=", 1) for line in path.read_text().splitlines() if "=" in line and not line.startswith("#"))
    return {key.strip(): value.strip().strip("\"'") for key, value in pairs}


def substitute(text: str, variables: dict[str, str]) -> str:
    """Replace known ${VAR} references; unknown ones stay literal, as postBuild values are not available."""
    return VARIABLE.sub(lambda m: variables.get(m[1], m[0]), text)


def kustomize_build(path: Path, variables: dict[str, str]) -> list[Doc]:
    out = subprocess.run(["kustomize", "build", str(path)], capture_output=True, text=True, check=True).stdout
    return [doc for doc in yaml.safe_load_all(substitute(out, variables)) if doc]


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
        command += ["--values", values_file.name]
        if kube_version:
            command += ["--kube-version", kube_version]
        out = subprocess.run(command, capture_output=True, text=True, check=True).stdout
    docs = [doc for doc in yaml.safe_load_all(out) if doc]
    if not (spec.get("test") or {}).get("enable"):
        docs = [doc for doc in docs if not is_test_hook(doc)]
    return docs, namespace


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
            return subprocess.run(["crane", *args], capture_output=True, text=True, check=True).stdout.strip()
        except subprocess.CalledProcessError as error:
            raise DigestError(f"{args[-1]}: {error.stderr.strip()}") from error

    def resolve(self, image: str) -> tuple[str, Platforms]:
        name, _, digest = image.partition("@")
        if digest:
            name = name.rsplit(":", 1)[0] if ":" in name.rsplit("/", 1)[-1] else name
        else:
            digest = self.run("digest", image)
        manifest = json.loads(self.run("manifest", f"{name}@{digest}"))
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
        image = row["image"] or "{name}@{version}".format(**row["chart"])
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
    charts = charts or RemoteCharts(checkout)
    registry = registry or Crane()
    variables = load_vars(args.vars) if args.vars else {}
    warnings = 0
    failed = False

    def warn(message: str) -> None:
        nonlocal warnings
        warnings += 1
        print(f"warning: {message}", file=stderr)

    built: dict[str, list[Doc]] = {}
    pending = list(args.root or discover_roots(checkout, args.deploy_dir))
    seen = set(pending)
    while pending:
        root = pending.pop(0)
        try:
            built[root] = kustomize_build(checkout / root, variables)
        except subprocess.CalledProcessError as error:
            failed = True
            print(f"error: {root}: kustomize build failed: {error.stderr.strip()}", file=stderr)
            continue
        for flux in built[root]:
            if flux.get("kind") != "Kustomization" or not str(flux.get("apiVersion")).startswith("kustomize.toolkit"):
                continue
            path = os.path.normpath((flux.get("spec") or {}).get("path") or ".")
            if not (checkout / path).is_dir():
                warn(f"{root}: Kustomization {ref(flux)['namespace']}/{ref(flux)['name']}: path {path} not in checkout")
            elif path not in seen:
                seen.add(path)
                pending.append(path)

    sources: dict[SourceKey, Doc] = {
        (doc["kind"], ref(doc)["namespace"], str(ref(doc)["name"])): doc
        for docs in built.values()
        for doc in docs
        if str(doc.get("apiVersion", "")).startswith("source.toolkit.fluxcd.io/")
    }

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
            except (FetchError, subprocess.CalledProcessError) as error:
                detail = error.stderr.strip() if isinstance(error, subprocess.CalledProcessError) else error
                warn(f"{where}: not rendered: {detail}")
                chart_spec = (release["spec"].get("chart") or {}).get("spec") or {}
                row = add(None, root, release, None, "helm-unrendered")
                row["chart"] = {"name": chart_spec.get("chart"), "version": chart_spec.get("version")}
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


class RemoteCharts:
    """Fetches charts from Flux sources using ambient helm, docker and git credentials."""

    def __init__(self, checkout: Path) -> None:
        self.checkout = checkout
        self.workdir = tempfile.TemporaryDirectory(prefix="image-map-")
        self.cache: dict[tuple[str, ...], Path] = {}
        try:
            origin = subprocess.run(
                ["git", "-C", str(checkout), "remote", "get-url", "origin"], capture_output=True, text=True, check=True
            ).stdout
            self.origin: str | None = git_url_key(origin)
        except (subprocess.CalledProcessError, FileNotFoundError):
            self.origin = None

    def run(self, *command: str) -> str:
        try:
            return subprocess.run(command, capture_output=True, text=True, check=True).stdout
        except subprocess.CalledProcessError as error:
            raise FetchError(f"{' '.join(command[:2])}: {error.stderr.strip()}") from error

    def source(self, sources: dict[SourceKey, Doc], kind: str, namespace: str | None, name: str) -> Doc:
        if (found := sources.get((kind, namespace, name))) is None:
            raise FetchError(f"{kind} {namespace}/{name} not found in rendered roots")
        return found

    def fetch(self, release: Doc, sources: dict[SourceKey, Doc]) -> Path:
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
            # Build dependencies on a copy, so the checkout is left untouched.
            copy = Path(self.workdir.name) / f"deps-{len(self.cache)}" / path.name
            shutil.copytree(path, copy)
            self.run("helm", "dependency", "build", str(copy))
            self.cache[(url, chart, "deps")] = path = copy
        return path


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
