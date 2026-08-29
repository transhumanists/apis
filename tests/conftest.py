"""pytest conftest: redirects rate_limit state to a temp dir for all tests."""
import json
import sys
import tempfile
from pathlib import Path

import pytest

# Build a patched version of rate_limit.py pointing at a temp dir
_this_dir = Path(__file__).parent
_rl_src = (_this_dir.parent / "rate_limit.py").read_text()

# Save the original STATE_FILE/CACHE_DIR strings to replace
ORIG_ROOT_DEF = 'ROOT = Path(__file__).parent\nSTATE_FILE = ROOT / "data" / "rate_limit_state.json"'
ORIG_CACHE_DEF = 'CACHE_DIR = ROOT / "data" / "rate_limit_cache"'

# Create a unique temp dir for this test session
_session_tmp = Path(tempfile.mkdtemp(prefix="rl_test_"))


def _patch(src):
    tmp_str = str(_session_tmp).replace("\\", "\\\\")
    return (
        src
        .replace(ORIG_ROOT_DEF, f'ROOT = Path(r"{tmp_str}")\nSTATE_FILE = ROOT / "state.json"')
        .replace(ORIG_CACHE_DEF, "CACHE_DIR = ROOT / 'cache'")
    )


# Write the patched module alongside the test file so it can be imported
_patched_path = _session_tmp / "rate_limit.py"
_patched_path.write_text(_patch(_rl_src))

# Prepend the temp dir to sys.path so `import rate_limit` finds the patched copy
sys.path.insert(0, str(_session_tmp))

# Remove any cached version already in sys.modules so we get the patched one
for key in list(sys.modules.keys()):
    if key == "rate_limit" or key.startswith("rate_limit."):
        del sys.modules[key]

# Now import the patched module
import rate_limit as rl_module  # noqa: E402

# Make it available as a fixture
@pytest.fixture
def rl():
    """Reset rate_limit state before each test."""
    rl_module.reset()
    yield rl_module
    rl_module.reset()


@pytest.fixture(scope="session", autouse=True)
def cleanup(request):
    """Remove temp dir after all tests finish."""
    def finalizer():
        import shutil
        shutil.rmtree(_session_tmp, ignore_errors=True)
    request.addfinalizer(finalizer)
