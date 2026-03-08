#!/usr/bin/env bash
# Run only bootstrap-context e2e tests with verbose live output.
#
# This mode creates a fresh outer VM and runs host-context e2e inside it.
# It intentionally disables host-context tests in the current machine to avoid
# duplicate execution.
#
# Prerequisites:
#   - libvirt/KVM with nested virtualization enabled
#   - passwordless sudo (sudo -n true)
#   - SSH keygen available

set -euo pipefail

export AIVM_E2E=1
export AIVM_E2E_BOOTSTRAP=1
export AIVM_E2E_HOST_CONTEXT=0
export AIVM_E2E_CLI_VERBOSITY="${AIVM_E2E_CLI_VERBOSITY:-2}"

pytest tests/test_e2e_bootstrap_context.py -s -v "$@"
