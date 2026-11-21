from image_map import extract_images


def pod(*images, key="containers"):
    return {key: [{"name": f"c{i}", "image": image} for i, image in enumerate(images)]}


def template(spec):
    return {"template": {"spec": spec}}


def found(docs):
    return [(doc["kind"], image, source) for doc, image, source in extract_images(docs, "kustomize")]


def test_reads_all_container_lists_of_a_pod():
    spec = {**pod("init", key="initContainers"), **pod("main"), **pod("debug", key="ephemeralContainers")}

    assert found([{"kind": "Pod", "spec": spec}]) == [
        ("Pod", "init", "kustomize"),
        ("Pod", "main", "kustomize"),
        ("Pod", "debug", "kustomize"),
    ]


def test_reads_pod_templates_of_every_workload_kind():
    docs = [
        {"kind": kind, "spec": template(pod(kind.lower()))}
        for kind in ("Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "Job")
    ]
    docs.append({"kind": "CronJob", "spec": {"jobTemplate": {"spec": template(pod("cron"))}}})

    assert [image for _, image, _ in found(docs)] == [
        "deployment",
        "statefulset",
        "daemonset",
        "replicaset",
        "job",
        "cron",
    ]


def test_finds_image_keys_anywhere_in_other_kinds():
    prometheus = {
        "kind": "Prometheus",
        "spec": {"image": "quay.io/prometheus/prometheus:v3", "containers": [{"image": "sidecar:1"}]},
    }

    assert found([prometheus]) == [
        ("Prometheus", "quay.io/prometheus/prometheus:v3", "generic"),
        ("Prometheus", "sidecar:1", "generic"),
    ]


def test_ignores_non_string_image_keys_and_imageless_kinds():
    docs = [
        {"kind": "Certificate", "spec": {"image": {"repository": "x", "tag": "1"}}},
        {"kind": "ConfigMap", "data": {"image": "not-really-but-string"}},
        {"kind": "Service", "spec": {}},
    ]

    assert found(docs) == [("ConfigMap", "not-really-but-string", "generic")]
