#!/bin/sh
# Bundle brainfreeze-studio, the copilot-harness-sdk tutorial assets, the code app host and azure-functions, then
# publish.
#   ./publish.sh <function app name> [path to copilot-harness-sdk]
# The code app host is built here with npm (Node 18+), which fetches Microsoft's @microsoft/power-apps SDK under its
# own license terms, so the SDK is never checked in; rapplication jobs publish it inside each code app. The repo's
# translations/ go too: rapplication jobs use their ports, laid on their recorded proofs when running agent code
# isn't allowed (and the Function has no .NET SDK to run connector code anyway).
set -eu
APP="$1"
SDK="${2:-../../../copilot-harness-sdk}"
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"
rm -rf brainfreeze_studio sdk .python_packages codeapp-host.js translations
cp -R ../../brainfreeze_studio brainfreeze_studio
cp -R ../../translations translations
find brainfreeze_studio -name __pycache__ -prune -exec rm -rf {} +
rm -rf brainfreeze_studio/codeapp_host/dist brainfreeze_studio/codeapp_host/node_modules
mkdir -p sdk && cp -R "$SDK/tutorial" sdk/tutorial
HOST="$(cd ../.. && python3 -c 'from brainfreeze_studio.codeapp import host_bundle; print(host_bundle())')"
cp "$HOST" codeapp-host.js
python3 -m pip install -q --disable-pip-version-check --target .python_packages/lib/site-packages -r requirements.txt
func azure functionapp publish "$APP" --python --no-build
