import pytest

from image_map import normalize_image

DIGEST = "sha256:" + "a" * 64


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        ("nginx", "docker.io/library/nginx:latest"),
        ("nginx:1.27", "docker.io/library/nginx:1.27"),
        ("bitnami/redis:7", "docker.io/bitnami/redis:7"),
        ("docker.io/nginx", "docker.io/library/nginx:latest"),
        ("ghcr.io/org/app:v1", "ghcr.io/org/app:v1"),
        ("localhost:5000/app", "localhost:5000/app:latest"),
        ("registry.local:5000/team/app:2", "registry.local:5000/team/app:2"),
        (f"ghcr.io/org/app@{DIGEST}", f"ghcr.io/org/app@{DIGEST}"),
        (f"nginx:1.27@{DIGEST}", f"docker.io/library/nginx:1.27@{DIGEST}"),
    ],
)
def test_normalizes_to_fully_qualified_ref(ref, expected):
    assert normalize_image(ref) == expected
