"""A module defining calculators for statistics."""
from __future__ import annotations

import asyncio
import dataclasses
import datetime
import decimal
import logging
from collections.abc import Callable
from collections.abc import Sequence
from typing import Any
from typing import final
from typing import Literal
from typing import Protocol
from typing import TypeVar

from homeassistant import exceptions
from homeassistant.components import recorder
from homeassistant.components.recorder import db_schema as recorder_db_schema
from homeassistant.components.recorder import statistics
from homeassistant.components.recorder import tasks
from homeassistant.components.recorder import util as recorder_util
from homeassistant.core import HomeAssistant

from . import const
from . import model
from . import state

_LOGGER = logging.getLogger(__name__)

T = TypeVar("T")

# Notes on statistic behavior:
#
# Sensor platform stat computations:
#
# Stats computed from the sensor state history. The values of `sum`, `state`,
# and `last_reset` in a stat record are the values at the *end* of the record
# period (non-inclusive). IOW, if a sensor value changed exactly at the end of
# the period, it won't be noticed in that period.
#
# What happens when the sensor resets in the middle of a stat record?
#   - When `last_reset` changes, it's assumed that the point in time when it
#     changes (not the value of `last_reset`) is the new zero point.
#   - If both `last_reset` and `state` changed at the same time, then it's
#     assumed that the reset happened first. The state at that time is
#     considered an additional sum.
#
# Recorder integration stat computations:
#
# Samples are stored with a start and end range.
#
# Code assumes that the values of `sum`, `min`, `max`, and `mean` are the same
# across the entire period. IOW, it assumes that they are the values at the
# *start* of the record period. Indeed, the UI seems to assume the same. It
# seems to be to be the opposite of the Sensor platform.
#
# However, code for compiling the hourly statistic reads the sum from the last
# 5m entry, which matches the Sensor platform. See
# `_compile_hourly_statistics()`.
#
# I guess the assumption is that the values stored was reached at some point in
# the period, and we don't know when, so we assume all points are the same as
# the end. This method over-estimates the value of the sample. Seems like a bug
# to me...
#
# General notes:
#   - Statistics are collected every 5 minutes and records the changes since
#     the last 5m.
#   - Works best when the first stat has a sum of 0, because the UI's "Adjust
#     Statistics" page can't modify the sum of the first stat.
#
# `statistics_during_period()`
#   - Returns all stats whose start time is within the range (non-inclusive
#     end). See `_statistics_during_period_stmt()`.
#   - Also returns the newest stat whose start time is before the range. See
#     `_statistics_at_time()`.
#
# `statistic_during_period()`
#   - Returns the change in stats within the period (non-inclusive end). See
#     `_get_newest_sum_statistic_in_sub_period()`.
#   - If the start time falls within a stat record period (non-inclusive
#     *start*), that record is considered the oldest. If the start time is equal
#     to a record start time, then the previous record is considered the oldest.
#     See `_get_oldest_sum_statistic_in_sub_period()`.
#   -
#


@final
@dataclasses.dataclass(frozen=True)
class _SensorStatRecord:
    timestamp: datetime.datetime
    last_reset: datetime.datetime | None
    state: decimal.Decimal
    sum: decimal.Decimal

    @classmethod
    def from_dict(cls, record: dict[str, Any]) -> _SensorStatRecord:
        """Create a stat record from the raw database record."""
        return _SensorStatRecord(
            timestamp=record["end"],
            last_reset=record.get("last_reset"),
            state=decimal.Decimal(record["state"]),
            sum=decimal.Decimal(record["sum"]),
        )

    def to_statistics_data(
        self, period: Literal["5minute", "hour"]
    ) -> statistics.StatisticData:
        """Create a StatisticData from this record."""
        return statistics.StatisticData(
            start=self.timestamp - _to_time_delta(period),
            last_reset=self.last_reset,
            state=float(self.state),
            sum=float(self.sum),
        )


@final
@dataclasses.dataclass(frozen=True)
class _StatisticSamples:
    prev_sum_before_end: float | None
    samples: list[_SensorStatRecord]

    def get_total_change(self) -> float:
        """Return the total change of the sum if these samples are applied."""
        if not self.samples:
            return 0.0
        if self.prev_sum_before_end is None:
            return float(self.samples[-1].sum)
        return float(self.samples[-1].sum) - self.prev_sum_before_end


@final
@dataclasses.dataclass(frozen=True)
class _MergedIntervalBlock:
    ids: list[str]
    reading_type: model.ReadingType
    start: datetime.datetime
    duration: datetime.timedelta
    interval_readings: list[model.IntervalReading]

    @property
    def end(self) -> datetime.datetime:
        """Return the block's interval's end time."""
        return self.start + self.duration

    @classmethod
    def create(cls, interval_blocks: list[model.IntervalBlock]) -> _MergedIntervalBlock:
        """Create a new merge block."""
        if not interval_blocks:
            raise ValueError("interval_blocks cannot be empty.")
        return cls(
            ids=[block.id for block in interval_blocks],
            reading_type=interval_blocks[0].reading_type,
            start=interval_blocks[0].start,
            duration=interval_blocks[-1].end - interval_blocks[0].start,
            interval_readings=[
                reading
                for block in interval_blocks
                for reading in block.interval_readings
            ],
        )


def _merge_interval_blocks(
    interval_blocks: Sequence[model.IntervalBlock],
) -> list[_MergedIntervalBlock]:
    res: list[_MergedIntervalBlock] = []
    merged_blocks: list[model.IntervalBlock] = []
    for curr_block in interval_blocks:
        if not merged_blocks:
            merged_blocks.append(curr_block)
            continue
        prev_block = merged_blocks[-1]
        if prev_block.end == curr_block.start:
            merged_blocks.append(curr_block)
            continue
        res.append(_MergedIntervalBlock.create(merged_blocks))
        merged_blocks = [curr_block]
    if merged_blocks:
        res.append(_MergedIntervalBlock.create(merged_blocks))
    return res


def _to_table(
    period: Literal["5minute", "hour"]
) -> type[recorder_db_schema.StatisticsShortTerm | recorder_db_schema.Statistics]:
    if period == "5minute":
        return recorder_db_schema.StatisticsShortTerm
    if period == "hour":
        return recorder_db_schema.Statistics


def _to_time_delta(period: Literal["5minute", "hour"]) -> datetime.timedelta:
    return _to_table(period).duration


def _round_down(
    datetime_val: datetime.datetime, period: Literal["5minute", "hour"]
) -> datetime.datetime:
    if period == "5minute":
        return datetime_val.replace(
            minute=datetime_val.minute - (datetime_val.minute % 5),
            second=0,
            microsecond=0,
        )
    if period == "hour":
        return datetime_val.replace(
            minute=0,
            second=0,
            microsecond=0,
        )


def _round_up(
    datetime_val: datetime.datetime, period: Literal["5minute", "hour"]
) -> datetime.datetime:
    return _round_down(
        datetime_val=datetime_val
        + _to_time_delta(period)
        - datetime.timedelta.resolution,
        period=period,
    )


def _is_aligned(
    datetime_val: datetime.datetime, period: Literal["5minute", "hour"]
) -> bool:
    return datetime_val == _round_down(datetime_val, period)


def _adjust_for_end_time(
    datetime_val: datetime.datetime, period: Literal["5minute", "hour"]
) -> datetime.datetime:
    """Return the start time of the stat record that would contain the datetime.

    If the datetime is on a period boundary, then return the previous period
    boundary. This is useful for treating stat records as representing the state
    of the world at the record's end time when query methods compare ranges
    against the start time (like the current state of the query methods).
    """
    return _round_down(datetime_val - datetime.timedelta.resolution, period)


def _queue_task(
    hass: HomeAssistant, task_ctor: Callable[[asyncio.Future[T]], tasks.RecorderTask]
) -> asyncio.Future[T]:
    future = asyncio.get_event_loop().create_future()
    recorder_util.get_instance(hass).queue_task(task_ctor(future))
    return future


def _complete_future(future: asyncio.Future[T], value: T) -> None:
    future.get_loop().call_soon_threadsafe(future.set_result, value)


class _StatsDao:
    def __init__(
        self,
        hass: HomeAssistant,
        statistic_id: str,
    ) -> None:
        self._hass = hass
        self._statistic_id = statistic_id

    def statistics_during_period_from_end_time(
        self,
        start: datetime.datetime,
        end: datetime.datetime,
        period: Literal["5minute", "hour"],
    ) -> list[_SensorStatRecord]:
        """Return the stats whose end time lies in the range (non-inclusive)."""
        # We adjust the range by subtracting the resolution and rounding down so
        # that the results return exactly the stat records whose end time lie in the
        # range.
        raw_data = statistics.statistics_during_period(
            hass=self._hass,
            start_time=_adjust_for_end_time(start, period),
            end_time=_adjust_for_end_time(end, period),
            statistic_ids=[self._statistic_id],
            period=period,
        ).get(self._statistic_id, [])
        if not raw_data:
            return []

        data = [_SensorStatRecord.from_dict(record) for record in raw_data]
        # Remove the head if the stat is before the requested range. This can
        # happen because `statistics_during_period` will attempt always attempt
        # to append the most recent stat record that starts before the requested
        # start time. It does this even if that record's end time is also before
        # the requested start time. Since we clamp the start time to the period,
        # this can only happen if there is a gap in data (e.g., if HASS is not
        # running when it should have collected that data point).
        if data[0].timestamp < start:
            return data[1:]
        return data

    def compute_sum_before(self, timestamp: datetime.datetime) -> _SensorStatRecord:
        """Compute the sum statistics before the specified time."""
        # We need to round up because the end time is non-inclusive.
        sample_datetime = _round_up(
            timestamp + datetime.timedelta.resolution, "5minute"
        )
        sum_before = statistics.statistic_during_period(
            hass=self._hass,
            start_time=None,
            end_time=sample_datetime,
            statistic_id=self._statistic_id,
            types={"change"},
            units=None,
        ).get("change")
        if sum_before is None:
            sum_before = 0
        sum_decimal = decimal.Decimal(sum_before)
        return _SensorStatRecord(
            timestamp=sample_datetime,
            last_reset=None,
            state=sum_decimal,
            sum=sum_decimal,
        )


class _ComputeUpdatedPeriodStatisticsTask(tasks.RecorderTask):
    def __init__(
        self,
        hass: HomeAssistant,
        statistic_id: str,
        data_extractor: DataExtractor,
        interval_block: _MergedIntervalBlock,
        period: Literal["5minute", "hour"],
        future: asyncio.Future[_StatisticSamples],
    ) -> None:
        self._hass = hass
        self._statistic_id = statistic_id
        self._data_extractor = data_extractor
        self._interval_block = interval_block
        self._period = period
        self._future = future

    def _statistics_during_period_from_end_time(
        self,
        start: datetime.datetime,
        end: datetime.datetime,
    ) -> list[_SensorStatRecord]:
        """Return the stats whose end time lies in the range (non-inclusive)."""
        # We adjust the range by subtracting the resolution and rounding down so
        # that the results return exactly the stat records whose end time lie in the
        # range.
        raw_data = statistics.statistics_during_period(
            hass=self._hass,
            start_time=_adjust_for_end_time(start, self._period),
            end_time=_adjust_for_end_time(end, self._period),
            statistic_ids=[self._statistic_id],
            period=self._period,
        ).get(self._statistic_id, [])
        if not raw_data:
            return []

        data = [_SensorStatRecord.from_dict(record) for record in raw_data]
        # Remove the head if the stat is before the requested range. This can
        # happen because `statistics_during_period` will attempt always attempt
        # to append the most recent stat record that starts before the requested
        # start time. It does this even if that record's end time is also before
        # the requested start time. Since we clamp the start time to the period,
        # this can only happen if there is a gap in data (e.g., if HASS is not
        # running when it should have collected that data point).
        if data[0].timestamp < start:
            return data[1:]
        return data

    def _compute_sum_before_old(
        self, timestamp: datetime.datetime
    ) -> _SensorStatRecord:
        # We need to round up because the end time is non-inclusive.
        sample_datetime = _round_up(
            timestamp + datetime.timedelta.resolution, "5minute"
        )
        sum_before = statistics.statistic_during_period(
            hass=self._hass,
            start_time=None,
            end_time=sample_datetime,
            statistic_id=self._statistic_id,
            types={"change"},
            units=None,
        ).get("change")
        if sum_before is None:
            sum_before = 0
        sum_decimal = decimal.Decimal(sum_before)
        return _SensorStatRecord(
            timestamp=sample_datetime,
            last_reset=None,
            state=sum_decimal,
            sum=sum_decimal,
        )

    def _compute_sum_before(self, timestamp: datetime.datetime) -> float:
        # We need to round up because the end time is non-inclusive.
        sample_datetime = _round_up(
            timestamp + datetime.timedelta.resolution, "5minute"
        )
        sum_before = statistics.statistic_during_period(
            hass=self._hass,
            start_time=None,
            end_time=sample_datetime,
            statistic_id=self._statistic_id,
            types={"change"},
            units=None,
        ).get("change")
        if sum_before is None:
            sum_before = 0
        return sum_before

    def _read_stats_and_generate_samples(
        self,
        start: datetime.datetime,
        end: datetime.datetime,
    ) -> tuple[datetime.timedelta, list[datetime.datetime]]:
        sample_period = _to_time_delta(self._period)
        if self._period == "hour":
            res = []
            sample_time = _round_up(start, self._period)
            # data_idx = 0
            while sample_time < end:
                res.append(sample_time)
                sample_time += sample_period
            return (sample_period, res)
        if self._period == "5minute":
            data = self._statistics_during_period_from_end_time(start, end)
            return (sample_period, [sample.timestamp for sample in data])

    def _compute_samples(
        self,
        start: datetime.datetime,
        end: datetime.datetime,
    ) -> _StatisticSamples:
        sample_period, sample_datetimes = self._read_stats_and_generate_samples(
            start=start, end=end
        )
        sum_before_start = self._compute_sum_before(start)
        prev_sum_before_end = None
        if sample_datetimes:
            prev_sum_before_end = self._compute_sum_before(sample_datetimes[-1])
        _LOGGER.debug(
            "[%s] Computing %s samples. Samples to compute: %d. Sum before start: %s. Prev sum before end: %s",
            self._statistic_id,
            self._period,
            len(sample_datetimes),
            sum_before_start,
            prev_sum_before_end,
        )

        reading_idx = 0
        curr_sum = decimal.Decimal(sum_before_start)
        reset_time = None
        res = []
        for i, sample_datetime in enumerate(sample_datetimes):
            if i > 0 and i % 10000 == 0:
                _LOGGER.debug(
                    "[%s] Finished computing %d samples", self._statistic_id, i
                )
            prev_sample_datetime = sample_datetime - sample_period
            curr_value = None
            while reading_idx < len(self._interval_block.interval_readings):
                reading = self._interval_block.interval_readings[reading_idx]
                if sample_datetime <= reading.start:
                    # Sample is fully before the reading.
                    break
                reading_value = self._data_extractor.get_native_value(reading)
                reading_period = reading.end - reading.start
                scale = decimal.Decimal(
                    (sample_datetime - reading.start) / reading_period
                )
                scale = min(scale, decimal.Decimal(1))
                reset_time = reading.start
                curr_value = scale * reading_value
                if prev_sample_datetime <= reading.start:
                    curr_sum += curr_value
                else:
                    prev_value_scale = decimal.Decimal(
                        (prev_sample_datetime - reading.start) / reading_period
                    )
                    prev_value_scale = min(prev_value_scale, decimal.Decimal(1))
                    curr_sum += curr_value - (prev_value_scale * reading_value)
                if sample_datetime < reading.end:
                    break
                reading_idx += 1

            if curr_value is not None:
                prev_sample = _SensorStatRecord(
                    timestamp=sample_datetime,
                    last_reset=reset_time,
                    state=curr_value,
                    sum=curr_sum,
                )
                res.append(prev_sample)
        stat_samples = _StatisticSamples(
            prev_sum_before_end=prev_sum_before_end, samples=res
        )
        if res:
            _LOGGER.debug(
                "[%s] Computed %d %s samples. Total change: %s. Latest sample:\n%s",
                self._statistic_id,
                len(res),
                self._period,
                stat_samples.get_total_change(),
                res[-1],
            )
        else:
            _LOGGER.debug(
                "[%s] No %s samples computed", self._statistic_id, self._period
            )
        return stat_samples

    def run(self, instance: recorder.Recorder) -> None:
        start = self._interval_block.start
        end = self._interval_block.end
        samples = self._compute_samples(start=start, end=end)
        _complete_future(self._future, samples)

    @classmethod
    def queue_task(
        cls,
        hass: HomeAssistant,
        statistic_id: str,
        data_extractor: DataExtractor,
        interval_block: _MergedIntervalBlock,
        period: Literal["5minute", "hour"],
    ) -> asyncio.Future[_StatisticSamples]:
        """Queue the task and return a future that completes when the task completes."""

        def ctor(
            future: asyncio.Future[_StatisticSamples],
        ) -> _ComputeUpdatedPeriodStatisticsTask:
            return cls(
                hass=hass,
                statistic_id=statistic_id,
                data_extractor=data_extractor,
                interval_block=interval_block,
                period=period,
                future=future,
            )

        return _queue_task(hass, ctor)


@final
@dataclasses.dataclass
class _ImportStatisticsTask(tasks.RecorderTask):
    hass: HomeAssistant
    entity: state.GreenButtonEntity
    samples: list[statistics.StatisticData]
    table: type[recorder_db_schema.StatisticsShortTerm | recorder_db_schema.Statistics]
    future: asyncio.Future[None]

    def run(self, instance: recorder.Recorder) -> None:
        statistic_id = self.entity.long_term_statistics_id
        _LOGGER.debug(
            "[%s] Importing %d statistics samples to table '%s'",
            statistic_id,
            len(self.samples),
            self.table.__tablename__,
        )
        metadata = statistics.get_metadata(self.hass, statistic_ids=[statistic_id]).get(
            statistic_id, (0, None)
        )[1]
        if metadata is None:
            metadata = create_metadata(self.entity)
        success = statistics.import_statistics(
            instance, metadata, self.samples, self.table
        )
        if not success:
            recorder_util.get_instance(self.hass).queue_task(self)
            return
        _complete_future(self.future, None)

    @classmethod
    def queue_task(
        cls,
        hass: HomeAssistant,
        entity: state.GreenButtonEntity,
        samples: list[statistics.StatisticData],
        table: type[statistics.Statistics | statistics.StatisticsShortTerm],
    ) -> asyncio.Future[None]:
        """Queue the task and return a future that completes when the task completes."""

        def ctor(future: asyncio.Future[None]) -> _ImportStatisticsTask:
            return cls(
                hass=hass,
                entity=entity,
                samples=samples,
                table=table,
                future=future,
            )

        return _queue_task(hass, ctor)


@final
@dataclasses.dataclass
class _AdjustStatisticsTask(tasks.RecorderTask):
    _MIN_CHANGE = decimal.Decimal(10) ** -10

    hass: HomeAssistant
    statistic_id: str
    start_time: datetime.datetime
    unit_of_measurement: str
    sum_adjustment: float
    future: asyncio.Future[None]

    def run(self, instance: recorder.Recorder) -> None:
        _LOGGER.debug(
            "[%s] Adjusting statistics after '%s' by %s %s",
            self.statistic_id,
            self.start_time,
            self.sum_adjustment,
            self.unit_of_measurement,
        )
        success = statistics.adjust_statistics(
            instance,
            self.statistic_id,
            self.start_time,
            float(self.sum_adjustment),
            self.unit_of_measurement,
        )
        if not success:
            recorder_util.get_instance(self.hass).queue_task(self)
            return
        _complete_future(self.future, None)

    @classmethod
    def queue_task(
        cls,
        hass: HomeAssistant,
        statistic_id: str,
        start_time: datetime.datetime,
        unit_of_measurement: str,
        sum_adjustment: float,
    ) -> asyncio.Future[None]:
        """Queue the task and return a Future that completes when the task is done."""

        def ctor(future: asyncio.Future[None]) -> _AdjustStatisticsTask:
            return cls(
                hass=hass,
                statistic_id=statistic_id,
                start_time=start_time,
                unit_of_measurement=unit_of_measurement,
                sum_adjustment=sum_adjustment,
                future=future,
            )

        return _queue_task(hass, ctor)


@final
@dataclasses.dataclass
class _ClearStatisticsTask(tasks.RecorderTask):
    hass: HomeAssistant
    statistic_id: str
    future: asyncio.Future[None]

    def run(self, instance: recorder.Recorder) -> None:
        _LOGGER.debug("[%s] Clearing statistics", self.statistic_id)
        statistics.clear_statistics(
            instance=instance, statistic_ids=[self.statistic_id]
        )
        _complete_future(self.future, None)

    @classmethod
    def queue_task(
        cls,
        hass: HomeAssistant,
        statistic_id: str,
    ) -> asyncio.Future[None]:
        """Queue the task and return a Future that completes when the task is done."""

        def ctor(future: asyncio.Future[None]) -> _ClearStatisticsTask:
            return cls(hass=hass, statistic_id=statistic_id, future=future)

        return _queue_task(hass, ctor)


class _UpdateStatisticsTask:
    def __init__(
        self,
        hass: HomeAssistant,
        stats_dao: _StatsDao,
        entity: state.GreenButtonEntity,
        data_extractor: DataExtractor,
        meter_reading: model.MeterReading,
    ) -> None:
        self._hass = hass
        self._stats_dao = stats_dao
        self._entity = entity
        self._data_extractor = data_extractor
        self._meter_reading = meter_reading

    @property
    def _statistic_id(self) -> str:
        return self._entity.long_term_statistics_id

    async def _update_statistics(
        self, interval_block: _MergedIntervalBlock, period: Literal["5minute", "hour"]
    ) -> _StatisticSamples:
        samples = await _ComputeUpdatedPeriodStatisticsTask.queue_task(
            hass=self._hass,
            statistic_id=self._statistic_id,
            data_extractor=self._data_extractor,
            interval_block=interval_block,
            period=period,
        )
        await _ImportStatisticsTask.queue_task(
            hass=self._hass,
            entity=self._entity,
            samples=[sample.to_statistics_data(period) for sample in samples.samples],
            table=_to_table(period),
        )
        if samples.samples and samples.get_total_change() != 0:
            await _AdjustStatisticsTask.queue_task(
                hass=self._hass,
                statistic_id=self._statistic_id,
                start_time=samples.samples[-1].timestamp,
                unit_of_measurement=self._entity.native_unit_of_measurement,
                sum_adjustment=samples.get_total_change(),
            )
        return samples

    async def _update_for_interval_block(
        self, interval_block: _MergedIntervalBlock
    ) -> None:
        _LOGGER.info(
            "[%s] Processing %d IntervalReadings for merged IntervalBlock from '%s' to '%s'",
            self._statistic_id,
            len(interval_block.interval_readings),
            interval_block.start,
            interval_block.end,
        )
        await self._update_statistics(interval_block, "hour")
        await self._update_statistics(interval_block, "5minute")

    async def __call__(self) -> None:
        _LOGGER.info("[%s] Updating statistics for entity", self._statistic_id)
        merged_blocks = _merge_interval_blocks(self._meter_reading.interval_blocks)
        for block in merged_blocks:
            if not _is_aligned(block.end, "hour"):
                raise UnalignedIntervalBlocksError(
                    f"Merged IntervalBlock not aligned at end time. Block ID: {repr(block.ids[-1])}. Block end: '{block.end}'"
                )
        for block in merged_blocks:
            await self._update_for_interval_block(block)
        _LOGGER.info("[%s] Statistics update complete", self._statistic_id)

    @classmethod
    def create(
        cls,
        hass: HomeAssistant,
        entity: state.GreenButtonEntity,
        data_extractor: DataExtractor,
        meter_reading: model.MeterReading,
    ) -> _UpdateStatisticsTask:
        """Create a new task."""
        return _UpdateStatisticsTask(
            hass=hass,
            stats_dao=_StatsDao(hass, entity.long_term_statistics_id),
            entity=entity,
            data_extractor=data_extractor,
            meter_reading=meter_reading,
        )


class UnalignedIntervalBlocksError(exceptions.HomeAssistantError):
    """An error raised when a MeterReading contains unaligned readings.

    Unaligned readings cannot be stored so has the potential to cause data
    corruption.
    """


class DataExtractor(Protocol):
    """A protocol for an instance that can extract data from an IntervalReading."""

    def get_native_value(
        self, interval_reading: model.IntervalReading
    ) -> decimal.Decimal:
        """Get the native value from the IntervalReading."""


def create_metadata(entity: state.GreenButtonEntity) -> statistics.StatisticMetaData:
    """Create the statistic metadata for the entity."""
    return {
        "has_mean": True,
        "has_sum": True,
        "name": entity.name,
        "source": const.DOMAIN,
        "statistic_id": entity.long_term_statistics_id,
        "unit_of_measurement": entity.native_unit_of_measurement,
    }


async def update_statistics(
    hass: HomeAssistant,
    entity: state.GreenButtonEntity,
    data_extractor: DataExtractor,
    meter_reading: model.MeterReading,
) -> None:
    """Update the statistics for an entry to match the MeterReading.

    This method is idempotent.
    """
    await _UpdateStatisticsTask.create(
        hass=hass,
        entity=entity,
        data_extractor=data_extractor,
        meter_reading=meter_reading,
    )()


async def clear_statistic(hass: HomeAssistant, statistic_id: str) -> None:
    """Clear all statistics with the specified ID."""
    await _ClearStatisticsTask.queue_task(hass=hass, statistic_id=statistic_id)
