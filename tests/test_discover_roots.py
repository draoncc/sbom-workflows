from image_map import discover_roots


def kustomization(path, **fields):
    path.mkdir(parents=True, exist_ok=True)
    lines = ["apiVersion: kustomize.config.k8s.io/v1beta1", "kind: Kustomization"]
    for key, entries in fields.items():
        lines.append(f"{key}:")
        lines += [f"  - {entry}" for entry in entries]
    (path / "kustomization.yaml").write_text("\n".join(lines) + "\n")


def test_unreferenced_kustomizations_are_roots(tmp_path):
    kustomization(tmp_path / "deploy/apps/web", resources=["../../base/web", "extra.yaml"])
    kustomization(tmp_path / "deploy/apps/api", resources=["https://github.com/org/repo//base"])
    kustomization(tmp_path / "deploy/base/web", components=["../../components/probes"])
    kustomization(tmp_path / "deploy/components/probes")
    kustomization(tmp_path / "deploy/legacy", bases=["../base/legacy"])
    kustomization(tmp_path / "deploy/base/legacy")
    kustomization(tmp_path / "elsewhere/ignored")

    assert discover_roots(tmp_path, "deploy") == ["deploy/apps/api", "deploy/apps/web", "deploy/legacy"]


def test_root_referenced_from_outside_deploy_dir_is_still_a_root(tmp_path):
    kustomization(tmp_path / "deploy/app")
    kustomization(tmp_path / "overlays/local", resources=["../../deploy/app"])

    assert discover_roots(tmp_path, "deploy") == ["deploy/app"]
