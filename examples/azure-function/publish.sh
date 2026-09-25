#!/bin/sh
# Bundle brainfreeze-studio, the copilot-harness-sdk tutorial assets and azure-functions, then publish.
#   ./publish.sh <function app name> [path to copilot-harness-sdk]
set -eu
APP="$1"
SDK="${2:-../../../copilot-harness-sdk}"
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"
rm -rf brainfreeze_studio sdk .python_packages
cp -R ../../brainfreeze_studio brainfreeze_studio
find brainfreeze_studio -name __pycache__ -prune -exec rm -rf {} +
mkdir -p sdk && cp -R "$SDK/tutorial" sdk/tutorial
python3 -m pip install -q --disable-pip-version-check --target .python_packages/lib/site-packages -r requirements.txt
func azure functionapp publish "$APP" --python --no-build
