import io
import json
import shutil
import subprocess

import pytest
import yaml

import image_map
from image_map import main


def run(argv, **kwargs):
    out, err = io.StringIO(), io.StringIO()
    code = main(argv, stdout=out, stderr=err, **kwargs)
    rows = [json.loads(line) for line in out.getvalue().splitlines()]
    return code, rows, err.getvalue()


def test_plain_deployment_maps_image_to_workload(fixtures):
    code, rows, _ = run([str(fixtures / "plain")])

    assert code == 0
    assert rows == [
        {
            "image": "docker.io/library/nginx:1.27",
            "root": "deploy/app",
            "controller": {"kind": "Deployment", "namespace": "web", "name": "nginx"},
            "workload": {"kind": "Deployment", "namespace": "web", "name": "nginx"},
            "source": "kustomize",
            "unresolved": False,
        }
    ]


class FakeCharts:
    """Serves charts from tests/fixtures/charts by chart name."""

    def __init__(self, charts_dir):
        self.charts_dir = charts_dir
        self.requests = []

    def fetch(self, release, sources):
        chart = release.chart_spec
        source_ref = chart["sourceRef"]
        source = sources.get(image_map.ObjectRef(source_ref["kind"], source_ref.get("namespace"), source_ref["name"]))
        self.requests.append((chart["chart"], chart.get("version"), source and source["spec"]["url"]))
        path = self.charts_dir / chart["chart"]
        if not path.is_dir():
            raise image_map.FetchError(f"no chart {chart['chart']}")
        return path


def test_helm_release_images_point_to_release_and_rendered_workload(fixtures):
    charts = FakeCharts(fixtures / "charts")

    code, rows, err = run([str(fixtures / "helm")], charts=charts)

    assert code == 0
    assert charts.requests == [("podinfo", "6.x", "https://stefanprodan.github.io/podinfo")]
    release = {"kind": "HelmRelease", "namespace": "apps", "name": "podinfo"}
    workload = {"kind": "Deployment", "namespace": "web", "name": "web-podinfo"}
    assert rows == [
        {
            "image": "ghcr.io/stefanprodan/podinfo:6.7.0",
            "root": "deploy/apps/podinfo",
            "controller": release,
            "workload": workload,
            "source": "helm",
            "unresolved": False,
        },
        {
            "image": "docker.io/library/busybox:1.36",
            "root": "deploy/apps/podinfo",
            "controller": release,
            "workload": workload,
            "source": "helm",
            "unresolved": False,
        },
    ]
    assert "Secret apps/podinfo-secret-values" in err


def test_unfetchable_chart_falls_back_to_unrendered_row_and_failed_build_exits_1(fixtures):
    code, rows, err = run([str(fixtures / "fallbacks")], charts=FakeCharts(fixtures / "charts"))

    assert code == 1
    assert rows == [
        {
            "image": None,
            "root": "deploy/missing-chart",
            "controller": {"kind": "HelmRelease", "namespace": "apps", "name": "private"},
            "workload": None,
            "source": "helm-unrendered",
            "unresolved": False,
            "chart": {"name": "private-chart", "version": ">=1.0.0"},
        }
    ]
    assert "deploy/broken" in err
    assert "no chart private-chart" in err


def test_fallbacks_only_exit_0_unless_strict(fixtures):
    argv = [str(fixtures / "fallbacks"), "--root", "deploy/missing-chart"]

    assert run(argv, charts=FakeCharts(fixtures / "charts"))[0] == 0
    assert run([*argv, "--strict"], charts=FakeCharts(fixtures / "charts"))[0] == 1


def test_unresolved_variables_are_kept_literally_and_duplicates_collapse(fixtures):
    code, rows, _ = run([str(fixtures / "vars")])

    assert code == 0
    assert [(row["image"], row["unresolved"]) for row in rows] == [
        ("${REGISTRY}/app:${TAG}", True),
        ("${PROXY_REGISTRY:=docker.io/org}/proxy:${PROXY_TAG:=1.0}", True),
        ("ghcr.io/org/escaped:${TAG}", False),
    ]


def test_vars_file_substitutes_known_variables_and_defaults(fixtures):
    argv = [str(fixtures / "vars"), "--vars", str(fixtures / "vars" / "vars.env")]

    _, rows, _ = run(argv)

    assert [(row["image"], row["unresolved"]) for row in rows] == [
        ("ghcr.io/org/app:${TAG}", True),
        ("docker.io/org/proxy:2.1", False),
        ("ghcr.io/org/escaped:${TAG}", False),
    ]
    assert run([*argv, "--strict"])[0] == 1


def test_follows_local_paths_of_rendered_flux_kustomizations(fixtures):
    code, rows, err = run([str(fixtures / "nested")])

    assert code == 0
    assert sorted((row["root"], row["image"], row["controller"]["namespace"]) for row in rows) == [
        ("deploy/apps/web", "docker.io/library/nginx:latest", "apps"),
        ("shared/plain-dir", "docker.io/library/alpine:latest", "apps"),
        ("shared/redis", "docker.io/library/redis:latest", "cache"),
    ]
    assert "flux-system/elsewhere" in err
    assert "flux-system/foreign" in err


def test_given_roots_are_normalized_before_deduping(fixtures):
    _, rows, _ = run([str(fixtures / "nested"), "--root", "./deploy/apps/web/", "--root", "deploy/clusters/prod"])

    assert sorted(row["root"] for row in rows) == ["deploy/apps/web", "shared/plain-dir", "shared/redis"]


def test_missing_kustomize_binary_fails_the_root_without_crashing(fixtures, monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))

    code, rows, err = run([str(fixtures / "plain")])

    assert (code, rows) == (1, [])
    assert "deploy/app" in err


def test_values_files_and_post_renderer_images_apply(fixtures, tmp_path):
    checkout = tmp_path / "checkout"
    shutil.copytree(fixtures / "helm", checkout)
    release = checkout / "deploy/apps/podinfo/helmrelease.yaml"
    release.write_text(
        release.read_text().replace("      version: 6.x\n", "      version: 6.x\n      valuesFiles: [values.yaml, values-mirror.yaml]\n")
        + """  postRenderers:
    - kustomize:
        images:
          - name: ghcr.io/org/podinfo-mirror
            newTag: 6.7.1
          - name: busybox
            newName: mirror.gcr.io/library/busybox
"""
    )

    code, rows, _ = run([str(checkout)], charts=FakeCharts(fixtures / "charts"))

    assert code == 0
    assert [row["image"] for row in rows] == ["ghcr.io/org/podinfo-mirror:6.7.1", "mirror.gcr.io/library/busybox:1.36"]


INDEX = "sha256:" + "1" * 64
AMD64 = "sha256:" + "2" * 64
ARM64 = "sha256:" + "3" * 64
SINGLE = "sha256:" + "4" * 64


class FakeRegistry:
    def __init__(self):
        self.lookups = []

    def resolve(self, image):
        self.lookups.append(image)
        if image.startswith("ghcr.io/stefanprodan/podinfo:"):
            return INDEX, [{"platform": "linux/amd64", "digest": AMD64}, {"platform": "linux/arm64", "digest": ARM64}]
        if image.startswith("docker.io/library/busybox:"):
            return SINGLE, None
        raise image_map.DigestError(f"{image}: not found")


def test_resolve_digests_adds_digest_and_platforms(fixtures):
    registry = FakeRegistry()
    argv = [str(fixtures / "helm"), "--resolve-digests"]

    code, rows, _ = run(argv, charts=FakeCharts(fixtures / "charts"), registry=registry)

    assert code == 0
    assert [(row["image"], row["digest"], row["platforms"]) for row in rows] == [
        (
            "ghcr.io/stefanprodan/podinfo:6.7.0",
            INDEX,
            [{"platform": "linux/amd64", "digest": AMD64}, {"platform": "linux/arm64", "digest": ARM64}],
        ),
        ("docker.io/library/busybox:1.36", SINGLE, None),
    ]


def test_failed_digest_lookup_warns_and_leaves_digest_null(fixtures):
    registry = FakeRegistry()
    argv = [str(fixtures / "vars"), "--vars", str(fixtures / "vars" / "vars.env"), "--resolve-digests"]

    code, rows, err = run(argv, registry=registry)

    assert code == 0
    assert registry.lookups == ["docker.io/org/proxy:2.1", "ghcr.io/org/escaped:${TAG}"]
    assert [(row["image"], row["digest"], row["platforms"]) for row in rows] == [
        ("ghcr.io/org/app:${TAG}", None, None),
        ("docker.io/org/proxy:2.1", None, None),
        ("ghcr.io/org/escaped:${TAG}", None, None),
    ]
    assert "docker.io/org/proxy:2.1: not found" in err


def test_table_format_lists_one_line_per_row(fixtures):
    out, err = io.StringIO(), io.StringIO()

    main([str(fixtures / "plain"), "--format", "table"], stdout=out, stderr=err)

    lines = out.getvalue().splitlines()
    assert lines[0].split() == ["IMAGE", "CONTROLLER", "WORKLOAD", "SOURCE", "ROOT"]
    assert lines[1].split() == [
        "docker.io/library/nginx:1.27",
        "Deployment/web/nginx",
        "Deployment/web/nginx",
        "kustomize",
        "deploy/app",
    ]


def test_helm_test_hooks_only_count_when_release_enables_tests(fixtures, tmp_path):
    checkout = tmp_path / "checkout"
    shutil.copytree(fixtures / "helm", checkout)
    release = checkout / "deploy/apps/podinfo/helmrelease.yaml"
    release.write_text(release.read_text().replace("  values:\n", "  test:\n    enable: true\n  values:\n"))

    _, without_tests, _ = run([str(fixtures / "helm")], charts=FakeCharts(fixtures / "charts"))
    _, with_tests, _ = run([str(checkout)], charts=FakeCharts(fixtures / "charts"))

    assert "docker.io/curlimages/curl:8.0.0" not in [row["image"] for row in without_tests]
    assert [row["workload"] for row in with_tests if row["image"] == "docker.io/curlimages/curl:8.0.0"] == [
        {"kind": "Pod", "namespace": "web", "name": "web-podinfo-test"}
    ]


def test_long_release_names_are_shortened_like_flux(fixtures, tmp_path):
    checkout = tmp_path / "checkout"
    shutil.copytree(fixtures / "helm", checkout)
    release = checkout / "deploy/apps/podinfo/helmrelease.yaml"
    long_namespace = "a-very-long-target-namespace-for-release-truncation"
    release.write_text(release.read_text().replace("targetNamespace: web", f"targetNamespace: {long_namespace}"))

    _, rows, _ = run([str(checkout)], charts=FakeCharts(fixtures / "charts"))

    assert {row["workload"]["name"] for row in rows} == {"a-very-long-target-namespace-for-release-95a9e7aacb4b"}


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def chart_repo(fixtures, tmp_path):
    """A git repository whose podinfo chart image tag names the ref it was committed on."""
    repo = tmp_path / "charts-repo"
    shutil.copytree(fixtures / "charts", repo / "charts")
    git("init", "-q", "-b", "master", cwd=repo)
    git("config", "user.email", "test@example.com", cwd=repo)
    git("config", "user.name", "test", cwd=repo)
    values = repo / "charts/podinfo/values.yaml"
    commits = {}

    def commit(label):
        values.write_text(f"image:\n  repository: ghcr.io/stefanprodan/podinfo\n  tag: {label}\nsidecar:\n  image: ''\n")
        git("add", ".", cwd=repo)
        git("commit", "-q", "-m", label, cwd=repo)
        commits[label] = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
        ).stdout.strip()

    commit("pinned-commit")
    commit("v1.0.0")
    git("tag", "v1.0.0", cwd=repo)
    commit("v1.1.0")
    git("tag", "v1.1.0", cwd=repo)
    commit("master")
    git("checkout", "-q", "-b", "main", cwd=repo)
    commit("main")
    git("checkout", "-q", "master", cwd=repo)
    return repo, commits


def git_release_checkout(tmp_path, repo, git_ref):
    checkout = tmp_path / "deploy-repo"
    (checkout / "deploy/app").mkdir(parents=True)
    (checkout / "deploy/app/kustomization.yaml").write_text("resources: [release.yaml]\n")
    source = {
        "apiVersion": "source.toolkit.fluxcd.io/v1",
        "kind": "GitRepository",
        "metadata": {"name": "charts", "namespace": "apps"},
        "spec": {"url": repo.as_uri(), **({"ref": git_ref} if git_ref else {})},
    }
    release = {
        "apiVersion": "helm.toolkit.fluxcd.io/v2",
        "kind": "HelmRelease",
        "metadata": {"name": "podinfo", "namespace": "apps"},
        "spec": {"chart": {"spec": {"chart": "charts/podinfo", "sourceRef": {"kind": "GitRepository", "name": "charts"}}}},
    }
    (checkout / "deploy/app/release.yaml").write_text(yaml.safe_dump_all([source, release]))
    return checkout


@pytest.mark.parametrize(
    ("git_ref", "expected_tag"),
    [
        (None, "master"),
        ({"branch": "main"}, "main"),
        ({"tag": "v1.0.0"}, "v1.0.0"),
        ({"semver": "1.x"}, "v1.1.0"),
        ({"name": "refs/tags/v1.0.0"}, "v1.0.0"),
        ({"commit": "pinned-commit"}, "pinned-commit"),
        ({"branch": "main", "tag": "v1.1.0"}, "v1.1.0"),
        ({"branch": "main", "commit": "pinned-commit"}, "pinned-commit"),
    ],
)
def test_git_repository_charts_are_cloned_at_flux_ref(chart_repo, tmp_path, git_ref, expected_tag):
    repo, commits = chart_repo
    if git_ref and "commit" in git_ref:
        git_ref = {**git_ref, "commit": commits[git_ref["commit"]]}

    code, rows, err = run([str(git_release_checkout(tmp_path, repo, git_ref))])

    assert code == 0, err
    assert [row["image"] for row in rows] == [f"ghcr.io/stefanprodan/podinfo:{expected_tag}"]
