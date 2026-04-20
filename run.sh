#!/usr/bin/env bash
# =============================================================================
# run.sh  –  Docksmith full demo script
#
# Performs:
#   0. Install the docksmith CLI into the uv venv
#   1. Download + import Alpine 3.18 base image (one-time setup)
#   2. Cold build  (all CACHE MISS)
#   3. Warm build  (all CACHE HIT)
#   4. Edit a source file → partial cache invalidation rebuild
#   5. docksmith images
#   6. docksmith run myapp:latest   (visible output)
#   7. docksmith run -e APP_NAME=Overridden myapp:latest  (env override)
#   8. Isolation test: file written inside container ≠ host
#   9. docksmith rmi myapp:latest
#
# Usage:
#   chmod +x run.sh
#   ./run.sh
# =============================================================================

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$REPO_DIR/.venv"
PYTHON="$VENV/bin/python3"
DS="$VENV/bin/docksmith"

SAMPLE="$REPO_DIR/sample"
ISOLATION_HOST_FILE="/tmp/isolation_test.txt"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

banner() {
    echo ""
    echo -e "${CYAN}${BOLD}══════════════════════════════════════════════════════${NC}"
    echo -e "${CYAN}${BOLD}  $1${NC}"
    echo -e "${CYAN}${BOLD}══════════════════════════════════════════════════════${NC}"
}

step() { echo -e "\n${YELLOW}▶ $1${NC}"; }
ok()   { echo -e "${GREEN}✔ $1${NC}"; }
fail() { echo -e "${RED}✘ $1${NC}"; exit 1; }

# =============================================================================
# 0. Install CLI
# =============================================================================
banner "Step 0 – Install docksmith into venv"

step "Installing package via uv sync"
cd "$REPO_DIR"
UV=$(command -v uv 2>/dev/null || echo "")
if [ -n "$UV" ]; then
    "$UV" sync --quiet
    ok "docksmith installed via uv at $DS"
else
    # uv not on PATH – try adding it from its default location
    if [ -x "$HOME/.local/bin/uv" ]; then
        "$HOME/.local/bin/uv" sync --quiet
        ok "docksmith installed via uv at $DS"
    else
        fail "uv not found. Please install uv: https://docs.astral.sh/uv/"
    fi
fi

# =============================================================================
# 1. Base-image setup
# =============================================================================
banner "Step 1 – Import Alpine 3.18 base image (one-time)"

step "Running setup_images.py (idempotent – reuses existing layer if present)…"
"$PYTHON" "$REPO_DIR/scripts/setup_images.py"
ok "Base image ready."

# =============================================================================
# 2. Cold build
# =============================================================================
banner "Step 2 – Cold build (expect all CACHE MISS)"

step "docksmith build -t myapp:latest ./sample  (with --no-cache to guarantee cold)"
"$DS" build --no-cache -t myapp:latest "$SAMPLE"
ok "Cold build complete."

# =============================================================================
# 3. Warm build
# =============================================================================
banner "Step 3 – Warm build (expect all CACHE HIT)"

step "docksmith build -t myapp:latest ./sample"
"$DS" build -t myapp:latest "$SAMPLE"
ok "Warm build complete."

# =============================================================================
# 4. Partial cache invalidation
# =============================================================================
banner "Step 4 – Edit source file → partial cache invalidation"

EDIT_FILE="$SAMPLE/run.sh"
EDIT_BACKUP="/tmp/docksmith_demo_run_sh.bak"

step "Appending a comment to sample/run.sh …"
cp "$EDIT_FILE" "$EDIT_BACKUP"
echo "# demo edit $(date)" >> "$EDIT_FILE"

step "Rebuilding …"
"$DS" build -t myapp:latest "$SAMPLE"
ok "Partial-invalidation build complete."

step "Restoring sample/run.sh …"
mv "$EDIT_BACKUP" "$EDIT_FILE"

# =============================================================================
# 5. docksmith images
# =============================================================================
banner "Step 5 – docksmith images"

"$DS" images
ok "Image listed."

# =============================================================================
# 6. Plain run
# =============================================================================
banner "Step 6 – docksmith run myapp:latest"

step "Running container …"
"$DS" run myapp:latest
ok "Container exited cleanly."

# =============================================================================
# 7. ENV override
# =============================================================================
banner "Step 7 – docksmith run with -e override"

step "docksmith run -e APP_NAME=Overridden myapp:latest"
"$DS" run -e APP_NAME=Overridden myapp:latest
ok "ENV override applied."

# =============================================================================
# 8. Isolation test
# =============================================================================
banner "Step 8 – Isolation test"

step "Removing stale isolation_test.txt from host (if any) …"
rm -f "$ISOLATION_HOST_FILE"

step "Running container (it writes /tmp/isolation_test.txt inside the container) …"
"$DS" run myapp:latest

step "Checking host for /tmp/isolation_test.txt …"
if [ -f "$ISOLATION_HOST_FILE" ]; then
    fail "ISOLATION FAILURE: $ISOLATION_HOST_FILE exists on the host!"
else
    echo ""
    echo -e "  ${GREEN}${BOLD}ISOLATION PASS${NC}: /tmp/isolation_test.txt does NOT exist on the host."
    ok "Filesystem isolation verified."
fi

# =============================================================================
# 9. docksmith rmi
# =============================================================================
banner "Step 9 – docksmith rmi myapp:latest"

step "docksmith rmi myapp:latest"
"$DS" rmi myapp:latest
ok "Image removed."

step "Verifying image is gone …"
LISTING=$("$DS" images 2>&1 || true)
if echo "$LISTING" | grep -q "myapp"; then
    fail "Image still listed after rmi!"
fi
ok "Image no longer listed."

step "Note: rmi also deleted the shared Alpine base layer (no ref-counting, per spec)."
step "Re-importing Alpine so the store is usable again …"
"$PYTHON" "$REPO_DIR/scripts/setup_images.py" 2>&1 | grep -E "Setup complete|Layer|Reusing|Image digest"
ok "Alpine re-imported."

# =============================================================================
# Done
# =============================================================================
banner "All demo steps passed ✔"
echo ""
echo "  Docksmith is working correctly."
echo "  See REPORT.md for design details."
echo ""
