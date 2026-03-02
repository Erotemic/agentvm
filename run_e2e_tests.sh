#!/usr/bin/env bash
# Run end-to-end tests with full verbosity and output capture disabled.
#
# Prerequisites:
#   - libvirt/KVM with nested virtualization enabled
#   - passwordless sudo (sudo -n true)
#   - SSH keygen available
#   - Optional: cached Ubuntu image at ~/.cache/aivm/e2e/noble-base.img

set -euo pipefail

export AIVM_E2E=1

# Run with -s (no capture) and -v (verbose) so you can watch progress in real-time.
pytest tests/test_e2e_*.py -s -v "$@"
