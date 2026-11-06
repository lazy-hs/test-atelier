import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is not installed")
def test_loopback_smoke_does_not_inherit_provider_secrets_or_node_preloads():
    root = Path(__file__).resolve().parents[2]
    script = """
import assert from 'node:assert/strict'
import { localSmokeEnvironment } from './scripts/harness_local_smoke_environment.mjs'
const source = {
  PATH: '/safe/bin', HOME: '/safe/home', SystemRoot: 'C:\\\\Windows',
  OPENAI_API_KEY: 'synthetic-openai', ANTHROPIC_API_KEY: 'synthetic-anthropic',
  GOOGLE_API_KEY: 'synthetic-google', NODE_OPTIONS: '--require=synthetic-preload',
  HARNESS_DSH_COMMAND: 'untrusted-runner', TEST_ATELIER_SCOPE_SECRET: 'synthetic-scope',
}
assert.deepEqual(localSmokeEnvironment(source), {
  PATH: '/safe/bin', HOME: '/safe/home', SystemRoot: 'C:\\\\Windows',
})
"""
    subprocess.run([shutil.which("node"), "--input-type=module", "-e", script], cwd=root, check=True)
