import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "flux-image-map.sh"
TABLE = "IMAGE  CONTROLLER\nnginx  Deployment/web/nginx\n"


@pytest.fixture
def runner(tmp_path):
    """Run the workflow step with a fake nix that records its arguments and prints TABLE."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "nix").write_text(
        '#!/usr/bin/env bash\nprintf "%s\\n" "$@" > "$NIX_ARGS"\nprintf "%s" "$FAKE_TABLE"\nexit "${FAKE_EXIT:-0}"\n'
    )
    (bin_dir / "nix").chmod(0o755)

    def run(exit_code=0, **inputs):
        env = {
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "RUNNER_TEMP": str(tmp_path),
            "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
            "NIX_ARGS": str(tmp_path / "nix-args"),
            "FAKE_TABLE": TABLE,
            "FAKE_EXIT": str(exit_code),
            "ROOTS": ".",
            "VARS": "",
            "KUBE_VERSION": "",
            "RESOLVE_DIGESTS": "false",
            "STRICT": "false",
            **inputs,
        }
        result = subprocess.run(["bash", "-e", str(SCRIPT), "tooling", "repo"], check=False, env=env, capture_output=True, text=True)
        args = (tmp_path / "nix-args").read_text().splitlines()
        script_args = args[args.index("tooling/scripts/image_map.py") + 1 :]
        return result, script_args

    return run


def test_each_root_line_becomes_a_root_flag(runner):
    _, args = runner(ROOTS="clusters/prod\n\n  \nclusters/staging\n")

    assert args == ["repo", "--format", "table", "--root", "clusters/prod", "--root", "clusters/staging"]


def test_blank_root_renders_the_repository_root_instead_of_discovering(runner):
    _, args = runner(ROOTS=" \n")

    assert args == ["repo", "--format", "table", "--root", "."]


def test_vars_and_flags_are_passed_through(runner, tmp_path):
    _, args = runner(VARS="REGISTRY=ghcr.io/org\n", KUBE_VERSION="1.31.0", RESOLVE_DIGESTS="true", STRICT="true")

    vars_file = str(tmp_path / "vars.env")
    assert args[5:] == ["--vars", vars_file, "--kube-version", "1.31.0", "--resolve-digests", "--strict"]
    assert Path(vars_file).read_text().split() == ["REGISTRY=ghcr.io/org"]


def test_table_goes_to_log_and_summary_even_when_the_script_fails(runner, tmp_path):
    result, _ = runner(exit_code=1)

    assert result.returncode == 1
    assert result.stdout == TABLE
    assert (tmp_path / "summary.md").read_text() == f"```\n{TABLE}```\n"
