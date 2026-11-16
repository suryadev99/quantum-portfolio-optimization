#!/usr/bin/env bash
# End-to-end local demo: validate -> dry-run -> run -> inspect. No credentials needed.
set -euo pipefail
qportfolio validate configs/demo_small.yaml
qportfolio dry-run configs/demo_small.yaml
qportfolio run configs/demo_small.yaml
qportfolio inspect outputs/demo_small
