#!/usr/bin/env bash
# Release flow for llm-lens.
#
# Usage:
#   ./release.sh test    # upload to TestPyPI
#   ./release.sh prod    # upload to real PyPI
#
# Tokens are loaded from .pypi-token (gitignored). Create it with:
#   TESTPYPI_TOKEN=pypi-xxxxxxxx
#   PYPI_TOKEN=pypi-xxxxxxxx

set -euo pipefail
cd "$(dirname "$0")"

TARGET="${1:-}"
if [[ "$TARGET" != "test" && "$TARGET" != "prod" ]]; then
    echo "Usage: $0 {test|prod}" >&2
    exit 2
fi

if [[ ! -f .pypi-token ]]; then
    echo "Error: .pypi-token not found. Create it with:" >&2
    echo "  TESTPYPI_TOKEN=pypi-xxxxxxxx" >&2
    echo "  PYPI_TOKEN=pypi-xxxxxxxx" >&2
    exit 1
fi

# shellcheck disable=SC1091
source .pypi-token

for tool in pyproject-build twine; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "Error: $tool not found. Install with: pipx install build twine" >&2
        exit 1
    fi
done

VERSION=$(grep -E '^version' pyproject.toml | head -1 | sed -E 's/.*"([^"]+)".*/\1/')
echo "Building llm-lens $VERSION..."

rm -rf dist/ build/
pyproject-build

echo
echo "Built artifacts:"
ls -la dist/

if [[ "$TARGET" == "test" ]]; then
    : "${TESTPYPI_TOKEN:?TESTPYPI_TOKEN not set in .pypi-token}"
    echo
    echo "Uploading to TestPyPI..."
    TWINE_USERNAME="__token__" TWINE_PASSWORD="$TESTPYPI_TOKEN" \
        twine upload --repository testpypi dist/*
    echo
    echo "Done. Smoke-test with (one line, no backslash):"
    echo "  pipx install --index-url https://test.pypi.org/simple/ --pip-args='--extra-index-url https://pypi.org/simple/' llm-lens"
else
    : "${PYPI_TOKEN:?PYPI_TOKEN not set in .pypi-token}"
    echo
    read -r -p "Upload llm-lens $VERSION to real PyPI? [y/N] " ans
    if [[ "$ans" != "y" && "$ans" != "Y" ]]; then
        echo "Aborted."
        exit 0
    fi
    TWINE_USERNAME="__token__" TWINE_PASSWORD="$PYPI_TOKEN" \
        twine upload dist/*
    echo
    echo "Done. Users can install with:"
    echo "  pipx install llm-lens"
fi
