"""Traceable historical market-data foundation."""

from .api import HistoricalDataService, load_bars
from .models import DataConflictError, DataLineageError, DataQualityError, HoldingsSnapshot, InstrumentConfig, InstrumentNotImplementedError, SurvivorshipBiasError
from .provider_factory import create_default_providers
from .registry import InstrumentRegistry
from .storage import open_data_lake

# 流水线（P1，additive）：既有导出全部保留且行为不变。
from .pipeline import (
    PIPELINE_STAGES,
    AcquireOutcome,
    DatasetKey,
    MarketDataPipeline,
    PipelineContext,
    PipelineRegistry,
    PipelineSpec,
    PublicationOutcome,
    TransformOutcome,
    get_pipeline,
    register_pipeline,
)

__all__ = ["HistoricalDataService", "HoldingsSnapshot", "InstrumentConfig", "InstrumentRegistry", "InstrumentNotImplementedError",
           "DataLineageError", "DataQualityError", "DataConflictError", "SurvivorshipBiasError", "load_bars",
           "create_default_providers", "open_data_lake",
           "PIPELINE_STAGES", "MarketDataPipeline", "PipelineSpec", "PipelineRegistry",
           "PipelineContext", "DatasetKey", "AcquireOutcome", "TransformOutcome",
           "PublicationOutcome", "register_pipeline", "get_pipeline"]
