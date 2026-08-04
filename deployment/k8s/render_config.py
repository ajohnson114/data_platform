#!/usr/bin/env python3
"""
Render the code locations' aws config files into the ConfigMaps that ship them.

WHY THIS EXISTS.
On EKS the ConfigMap is subPath-mounted over the config file baked into the
image, so the ConfigMap -- not the file in the repo -- is what the code location
actually reads. That made the manifests a hand-maintained second copy of a file
that already exists, and the two drifted: the ConfigMaps were missing keys the
code had been reading for some time. On a laptop that is invisible, because
`make` reads config.dev.yaml. It surfaces in the cluster, as a KeyError thrown
from inside an asset at materialisation time -- exactly the failure class this
repo keeps trying to move earlier.

Generating removes the copy. `--check` makes the drift a CI failure rather than
a deploy-time surprise, which is the point: nothing here prevents someone
editing the ConfigMap by hand, so the guard has to be that CI notices.

    python deployment/k8s/render_config.py            # rewrite the manifests
    python deployment/k8s/render_config.py --check    # exit 1 if they are stale

The ${...} placeholders pass through untouched; aws_up.sh substitutes them
before kubectl apply.
"""
import argparse
import difflib
import pathlib
import sys
import textwrap

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

# (manifest, ConfigMap name, source config)
TARGETS = [
    (
        "deployment/k8s/12-etl-config.yaml",
        "etl-pipeline-config",
        "code_locations/etl_pipeline/config/config/config.aws.yaml",
    ),
    (
        "deployment/k8s/14-ml-config.yaml",
        "ml-pipeline-config",
        "code_locations/basic_ml_pipeline/config/config/config.aws.yaml",
    ),
]

HEADER = """# GENERATED FROM {source}
#
# This ConfigMap is subPath-mounted over the config file baked into the image,
# so on EKS it -- not the file in the repo -- is what the code location reads.
# Keeping a hand-edited second copy here is how the two drift, and they had:
# this file was missing several keys the code had already started reading,
# which on EKS is a KeyError at materialisation time inside the cluster.
#
# Regenerate rather than edit:
#     python deployment/k8s/render_config.py
#
# The ${{...}} placeholders are substituted by aws_up.sh before kubectl apply.
apiVersion: v1
kind: ConfigMap
metadata:
  name: {name}
  namespace: data-platform
data:
  config.aws.yaml: |
"""


def render(name: str, source: str) -> str:
    body = (REPO_ROOT / source).read_text().rstrip("\n")
    return HEADER.format(source=source, name=name) + textwrap.indent(body, "    ") + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero if any manifest differs from what would be rendered",
    )
    args = parser.parse_args()

    stale = []
    for manifest, name, source in TARGETS:
        path = REPO_ROOT / manifest
        expected = render(name, source)
        current = path.read_text() if path.exists() else ""

        if current == expected:
            print(f"ok      {manifest}")
            continue

        if args.check:
            stale.append(manifest)
            print(f"STALE   {manifest} (source: {source})")
            # The diff is the useful part of a CI failure -- without it the
            # reader has to run the renderer locally to find out what moved.
            sys.stdout.writelines(
                difflib.unified_diff(
                    current.splitlines(keepends=True),
                    expected.splitlines(keepends=True),
                    fromfile=f"{manifest} (committed)",
                    tofile=f"{manifest} (rendered)",
                )
            )
        else:
            path.write_text(expected)
            print(f"written {manifest}")

    if stale:
        print(
            f"\n{len(stale)} manifest(s) out of date. "
            f"Run: python deployment/k8s/render_config.py",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
