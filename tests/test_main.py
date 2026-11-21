import io
import json
import shutil

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
        chart = release["spec"]["chart"]["spec"]
        source_ref = chart["sourceRef"]
        source = sources.get((source_ref["kind"], source_ref.get("namespace"), source_ref["name"]))
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
        ("${REGISTRY}/proxy:${PROXY_TAG:=1.0}", True),
    ]


def test_vars_file_substitutes_known_variables(fixtures):
    argv = [str(fixtures / "vars"), "--vars", str(fixtures / "vars" / "vars.env")]

    code, rows, _ = run(argv)

    assert [(row["image"], row["unresolved"]) for row in rows] == [
        ("ghcr.io/org/app:${TAG}", True),
        ("ghcr.io/org/proxy:2.1", False),
    ]
    assert run([*argv, "--strict"])[0] == 1


def test_follows_local_paths_of_rendered_flux_kustomizations(fixtures):
    code, rows, err = run([str(fixtures / "nested")])

    assert code == 0
    assert [(row["root"], row["image"]) for row in rows] == [
        ("deploy/apps/web", "docker.io/library/nginx:latest"),
        ("shared/redis", "docker.io/library/redis:latest"),
    ]
    assert "flux-system/elsewhere" in err


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
    assert registry.lookups == ["ghcr.io/org/proxy:2.1"]
    assert [(row["image"], row["digest"], row["platforms"]) for row in rows] == [
        ("ghcr.io/org/app:${TAG}", None, None),
        ("ghcr.io/org/proxy:2.1", None, None),
    ]
    assert "ghcr.io/org/proxy:2.1: not found" in err


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
