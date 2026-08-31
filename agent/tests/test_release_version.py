import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("release_version", Path(__file__).parents[1] / "packaging/release_version.py")
versioning = importlib.util.module_from_spec(spec)
spec.loader.exec_module(versioning)


def test_ci_versions_are_unique_for_runs_and_retries(tmp_path):
    assert versioning.ci_version(42, 1) == "0.6.42+1"
    assert len({versioning.ci_version(run, attempt) for run in (41, 42) for attempt in (1, 2)}) == 4
    source = tmp_path / "cmdb_agent"
    source.mkdir()
    (source / "__init__.py").write_text('__version__ = "0.5.10"\nSCHEMA_VERSION = 1\n')
    versioning.write_version(tmp_path, "0.6.42+1")
    assert (source / "__init__.py").read_text() == '__version__ = "0.6.42+1"\nSCHEMA_VERSION = 1\n'


@pytest.mark.parametrize("run,attempt", [(0, 1), (1, 0), ("bad", 1), (65536, 1), (1, 65536)])
def test_ci_invalid_numbers_rejected(run, attempt):
    with pytest.raises(ValueError):
        versioning.ci_version(run, attempt)
