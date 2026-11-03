from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from fxnext.data.store import Dataset, load_dataset, write_dataset
from fxnext.data.synthetic import synthetic_m15
from fxnext.domain.enums import DatasetProvenance, PriceBasis

START = datetime(2024, 1, 1, tzinfo=UTC)
END = datetime(2024, 3, 1, tzinfo=UTC)


def make_dataset(
    root: Path,
    *,
    start: datetime = START,
    end: datetime = END,
    symbol: str = "EURUSD",
    **kwargs: object,
) -> Dataset:
    result = synthetic_m15(start, end, **kwargs)  # type: ignore[arg-type]
    ref = write_dataset(
        root,
        result,
        symbol=symbol,
        basis=PriceBasis.BID,
        provenance=DatasetProvenance.SYNTHETIC,
        coverage_start=start,
        coverage_end=end,
        source={"kind": "synthetic"},
    )
    return load_dataset(root, ref.dataset_id)


@pytest.fixture
def dataset(tmp_path: Path) -> Dataset:
    return make_dataset(tmp_path / "ds")
