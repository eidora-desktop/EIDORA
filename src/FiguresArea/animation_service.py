from __future__ import annotations

from collections.abc import Iterable
from typing import cast
from uuid import uuid4

from FiguresArea.domain import (
    AnimationFrame,
    AnimationPlaybackAdvance,
    AnimationRepeatStyle,
    SlicerAnimationFrame,
)


class AnimationSequenceService:
    """Qt-free ordering, slicer generation, and playback rules."""

    _REPEAT_STYLES = frozenset({"from_start", "ping_pong", "play_once"})

    @classmethod
    def normalize_repeat_style(cls, value: object) -> AnimationRepeatStyle:
        normalized = str(value or "").strip().lower().replace(" ", "_")
        if normalized in cls._REPEAT_STYLES:
            return cast(AnimationRepeatStyle, normalized)
        return "from_start"

    @staticmethod
    def inserted(
        frames: Iterable[AnimationFrame],
        frame: AnimationFrame,
        index: int,
    ) -> list[AnimationFrame]:
        ordered = list(frames)
        ordered.insert(min(max(int(index), 0), len(ordered)), frame)
        return ordered

    @staticmethod
    def reordered(
        frames: Iterable[AnimationFrame], frame_ids: Iterable[str]
    ) -> list[AnimationFrame]:
        frame_map = {frame.frame_id: frame for frame in frames}
        return [frame_map[frame_id] for frame_id in frame_ids if frame_id in frame_map]

    @staticmethod
    def frame_by_id(
        frames: Iterable[AnimationFrame], frame_id: str
    ) -> AnimationFrame | None:
        return next((frame for frame in frames if frame.frame_id == frame_id), None)

    @classmethod
    def build_slicer_frames(
        cls,
        *,
        preset_path: str,
        preset_label: str,
        preset_payload: object,
        available_values: Iterable[object],
    ) -> list[SlicerAnimationFrame]:
        if not isinstance(preset_payload, dict):
            raise ValueError("The selected slicer preset is invalid.")
        payload = (
            preset_payload.get("slicer")
            if isinstance(preset_payload.get("slicer"), dict)
            else preset_payload
        )
        column = str(payload.get("column") or "").strip()
        if not column:
            raise ValueError("The selected slicer preset has no metadata column.")
        values = cls._sorted_values(available_values)
        if not values:
            raise ValueError(f"Slicer column '{column}' contains no values.")
        spread = cls._positive_int(payload.get("spread"), default=1)
        step = cls._positive_int(payload.get("step"), default=1)
        numeric_values = cls._integer_values(values)
        frames: list[SlicerAnimationFrame] = []
        if numeric_values is not None:
            start = cls._bounded_int(
                payload.get("start"), numeric_values[0], numeric_values[-1], numeric_values[0]
            )
            maximum = max(0, numeric_values[-1] - start - spread)
            for offset in cls._positions(maximum, step):
                range_start = start + offset
                range_end = range_start + spread
                active = tuple(
                    value
                    for value, numeric in zip(values, numeric_values, strict=True)
                    if range_start <= numeric <= range_end
                )
                frames.append(
                    cls._slicer_frame(
                        preset_path=preset_path,
                        preset_label=preset_label,
                        column=column,
                        active_values=active,
                        range_start=str(range_start),
                        range_end=str(range_end),
                    )
                )
            return frames

        start = cls._bounded_int(payload.get("start"), 0, len(values) - 1, 0)
        maximum = max(0, len(values) - start - spread)
        for offset in cls._positions(maximum, step):
            window_start = min(start + offset, len(values) - 1)
            active = tuple(values[window_start : window_start + spread])
            if not active:
                continue
            frames.append(
                cls._slicer_frame(
                    preset_path=preset_path,
                    preset_label=preset_label,
                    column=column,
                    active_values=active,
                    range_start=active[0],
                    range_end=active[-1],
                )
            )
        return frames

    @staticmethod
    def _slicer_frame(
        *,
        preset_path: str,
        preset_label: str,
        column: str,
        active_values: tuple[str, ...],
        range_start: str,
        range_end: str,
    ) -> SlicerAnimationFrame:
        range_label = (
            range_start if range_start == range_end else f"{range_start} – {range_end}"
        )
        return SlicerAnimationFrame(
            frame_id=uuid4().hex,
            label=f"{preset_label}: {range_label}",
            slicer_preset_path=preset_path,
            column=column,
            active_values=active_values,
            range_start=range_start,
            range_end=range_end,
        )

    @staticmethod
    def _positions(maximum: int, step: int) -> list[int]:
        maximum = max(0, int(maximum))
        positions = list(range(0, maximum + 1, max(1, int(step))))
        if positions[-1] != maximum:
            positions.append(maximum)
        return positions

    @staticmethod
    def _sorted_values(values: Iterable[object]) -> list[str]:
        unique = {str(value) for value in values}
        numeric: list[tuple[float, str]] = []
        for value in unique:
            try:
                numeric.append((float(value), value))
            except ValueError:
                return sorted(unique, key=str.lower)
        return [value for _number, value in sorted(numeric, key=lambda pair: pair[0])]

    @staticmethod
    def _integer_values(values: list[str]) -> list[int] | None:
        output: list[int] = []
        for value in values:
            try:
                numeric = float(value)
            except ValueError:
                return None
            if not numeric.is_integer():
                return None
            output.append(int(numeric))
        return output

    @staticmethod
    def _positive_int(value: object, *, default: int) -> int:
        try:
            return max(1, int(value))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _bounded_int(value: object, minimum: int, maximum: int, default: int) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError):
            number = default
        return min(max(number, minimum), maximum)

    @classmethod
    def advance(
        cls,
        *,
        index: int,
        count: int,
        repeat_style: object,
        direction: int,
    ) -> AnimationPlaybackAdvance:
        if count < 2:
            return AnimationPlaybackAdvance(max(index, 0), 1, finished=True)
        current = min(max(int(index), 0), count - 1)
        style = cls.normalize_repeat_style(repeat_style)
        if style == "play_once":
            if current >= count - 1:
                return AnimationPlaybackAdvance(current, 1, finished=True)
            return AnimationPlaybackAdvance(current + 1, 1)
        if style == "ping_pong":
            if current >= count - 1:
                next_direction = -1
            elif current <= 0:
                next_direction = 1
            else:
                next_direction = -1 if direction < 0 else 1
            return AnimationPlaybackAdvance(current + next_direction, next_direction)
        return AnimationPlaybackAdvance((current + 1) % count, 1)

    @classmethod
    def export_frames(
        cls,
        frames: Iterable[AnimationFrame],
        repeat_style: object,
    ) -> list[AnimationFrame]:
        ordered = list(frames)
        if (
            cls.normalize_repeat_style(repeat_style) == "ping_pong"
            and len(ordered) > 2
        ):
            return [*ordered, *ordered[-2:0:-1]]
        return ordered


SnapshotAnimationSequenceService = AnimationSequenceService
