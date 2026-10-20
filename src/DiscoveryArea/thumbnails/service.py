from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
import threading
from typing import Callable

from PIL import Image

from DiscoveryArea.thumbnails.cache import CacheKey, LruBytesCache
from DiscoveryArea.thumbnails.resolver import ThumbnailRef, ThumbnailResolver


@dataclass(frozen=True)
class DecodeRequest:
    metadata_id: int
    target_px: int
    variant_id: str | None = None
    prefer_original: bool = False


@dataclass(frozen=True)
class DecodedThumbnail:
    metadata_id: int
    target_px: int
    width: int
    height: int
    rgba: bytes
    variant_id: str | None


@dataclass(frozen=True)
class ThumbnailDecodeFailure:
    metadata_id: int
    target_px: int
    variant_id: str | None
    reason: str
    permanent: bool = True


@dataclass(frozen=True)
class ThumbnailSourceBatch:
    revision: int
    variant_id: str | None
    target_px: int
    requested_metadata_ids: tuple[int, ...]
    sources: tuple[ThumbnailRef, ...]
    missing_metadata_ids: tuple[int, ...]
    failed_message: str = ""


class ThumbnailService:
    """Async thumbnail decode/cache service without Qt dependencies."""

    def __init__(
        self,
        resolver: ThumbnailResolver,
        max_cache_bytes: int = 512 * 1024 * 1024,
        max_workers: int = 2,
    ) -> None:
        self._resolver = resolver
        self._cache = LruBytesCache(max_cache_bytes)
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="discovery-thumb")
        self._callbacks: list[Callable[[DecodedThumbnail | ThumbnailDecodeFailure | None], None]] = []
        self._source_callbacks: list[Callable[[ThumbnailSourceBatch], None]] = []
        self._source_cache: dict[tuple[str, int, int], ThumbnailRef | None] = {}
        self._source_cache_token: tuple[object, ...] | None = None
        self._in_flight: dict[tuple[str, int | None, int], list[DecodeRequest]] = {}
        self._in_flight_lock = threading.Lock()
        self._resolver_lock = threading.Lock()

    def subscribe_thumbnail_ready(
        self,
        callback: Callable[[DecodedThumbnail | ThumbnailDecodeFailure | None], None],
    ) -> None:
        if callback not in self._callbacks:
            self._callbacks.append(callback)

    def unsubscribe_thumbnail_ready(
        self,
        callback: Callable[[DecodedThumbnail | ThumbnailDecodeFailure | None], None],
    ) -> None:
        if callback in self._callbacks:
            self._callbacks.remove(callback)

    def subscribe_source_batch_ready(self, callback: Callable[[ThumbnailSourceBatch], None]) -> None:
        if callback not in self._source_callbacks:
            self._source_callbacks.append(callback)

    def unsubscribe_source_batch_ready(self, callback: Callable[[ThumbnailSourceBatch], None]) -> None:
        if callback in self._source_callbacks:
            self._source_callbacks.remove(callback)

    def request(self, request: DecodeRequest) -> ThumbnailRef | None:
        if request.prefer_original:
            self._executor.submit(self._resolve_original_and_request, request)
            return None
        ref = self._resolver.resolve(
            request.metadata_id,
            variant_id=request.variant_id,
            target_px=request.target_px,
        )
        return self._request_resolved(request, ref)

    def _resolve_original_and_request(self, request: DecodeRequest) -> None:
        try:
            with self._resolver_lock:
                ref = self._resolver.resolve_original(
                    request.metadata_id,
                    variant_id=request.variant_id,
                )
        except Exception:
            ref = None
        self._request_resolved(request, ref)

    def _request_resolved(
        self,
        request: DecodeRequest,
        ref: ThumbnailRef | None,
    ) -> ThumbnailRef | None:
        if ref is None:
            self._emit_thumbnail_ready(
                ThumbnailDecodeFailure(
                    metadata_id=int(request.metadata_id),
                    target_px=max(1, int(request.target_px)),
                    variant_id=request.variant_id,
                    reason="source_missing",
                )
            )
            return None
        target_px = int(request.target_px)
        key = CacheKey(path=f"{request.variant_id or ''}:{ref.path}", mtime_ns=ref.mtime_ns, target_px=target_px)
        cached = self._cache.get(key)
        if cached is not KeyError:
            payload = self._decode_cached_payload(
                metadata_id=request.metadata_id,
                target_px=target_px,
                variant_id=request.variant_id,
                raw=cached,
            )
            self._emit_thumbnail_ready(payload)
            return ref
        flight_key = (str(ref.path), ref.mtime_ns, target_px)
        with self._in_flight_lock:
            if flight_key in self._in_flight:
                self._in_flight[flight_key].append(request)
                return ref
            self._in_flight[flight_key] = [request]
        self._executor.submit(
            self._decode_and_publish,
            ref.path,
            target_px,
            ref.mtime_ns,
            flight_key,
        )
        return ref

    def request_sources(
        self,
        *,
        revision: int,
        metadata_ids: list[int] | tuple[int, ...],
        variant_id: str | None,
        target_px: int,
    ) -> None:
        requested = tuple(dict.fromkeys(int(value) for value in metadata_ids))
        if not requested:
            self._emit_source_batch(
                ThumbnailSourceBatch(
                    revision=int(revision),
                    variant_id=variant_id,
                    target_px=max(1, int(target_px)),
                    requested_metadata_ids=(),
                    sources=(),
                    missing_metadata_ids=(),
                )
            )
            return
        self._executor.submit(
            self._resolve_sources_and_publish,
            int(revision),
            requested,
            variant_id,
            max(1, int(target_px)),
        )

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    def clear_source_cache(self) -> None:
        with self._resolver_lock:
            self._source_cache.clear()
            self._source_cache_token = None

    def _decode_and_publish(
        self,
        path: Path,
        target_px: int,
        mtime_ns: int | None,
        flight_key: tuple[str, int | None, int],
    ) -> None:
        try:
            payload = self._decode_rgba(path, target_px)
            if payload is None:
                payload = self._decode_rgba(path, target_px)
            with self._in_flight_lock:
                waiters = tuple(self._in_flight.get(flight_key, ()))
            for request in waiters:
                key = CacheKey(
                    path=f"{request.variant_id or ''}:{path}",
                    mtime_ns=mtime_ns,
                    target_px=target_px,
                )
                self._cache.put(key, payload)
                decoded = self._decode_cached_payload(
                    metadata_id=request.metadata_id,
                    target_px=target_px,
                    variant_id=request.variant_id,
                    raw=payload,
                )
                if decoded is None:
                    self._emit_thumbnail_ready(
                        ThumbnailDecodeFailure(
                            metadata_id=int(request.metadata_id),
                            target_px=int(target_px),
                            variant_id=request.variant_id,
                            reason="decode_failed",
                        )
                    )
                else:
                    self._emit_thumbnail_ready(decoded)
        finally:
            with self._in_flight_lock:
                self._in_flight.pop(flight_key, None)

    def _resolve_sources_and_publish(
        self,
        revision: int,
        requested: tuple[int, ...],
        variant_id: str | None,
        target_px: int,
    ) -> None:
        try:
            with self._resolver_lock:
                token = self._resolver.source_catalog_token(variant_id)
                if token != self._source_cache_token:
                    self._source_cache.clear()
                    self._source_cache_token = token
                resolved: dict[int, ThumbnailRef] = {}
                unresolved: list[int] = []
                variant_key = str(variant_id or "")
                for metadata_id in requested:
                    cache_key = (variant_key, int(target_px), int(metadata_id))
                    if cache_key not in self._source_cache:
                        unresolved.append(metadata_id)
                        continue
                    cached = self._source_cache[cache_key]
                    if cached is None:
                        continue
                    try:
                        current_mtime = int(cached.path.stat().st_mtime_ns)
                    except OSError:
                        self._source_cache.pop(cache_key, None)
                        unresolved.append(metadata_id)
                        continue
                    if cached.mtime_ns is not None and current_mtime != int(cached.mtime_ns):
                        self._source_cache.pop(cache_key, None)
                        unresolved.append(metadata_id)
                        continue
                    resolved[metadata_id] = cached
                if unresolved:
                    newly_resolved = self._resolver.resolve_many(
                        unresolved,
                        variant_id=variant_id,
                        target_px=target_px,
                    )
                    for metadata_id in unresolved:
                        cache_key = (variant_key, int(target_px), int(metadata_id))
                        value = newly_resolved.get(metadata_id)
                        self._source_cache[cache_key] = value
                        if value is not None:
                            resolved[metadata_id] = value
            sources = tuple(resolved[metadata_id] for metadata_id in requested if metadata_id in resolved)
            missing = tuple(metadata_id for metadata_id in requested if metadata_id not in resolved)
            result = ThumbnailSourceBatch(
                revision=revision,
                variant_id=variant_id,
                target_px=target_px,
                requested_metadata_ids=requested,
                sources=sources,
                missing_metadata_ids=missing,
            )
        except Exception as exc:
            result = ThumbnailSourceBatch(
                revision=revision,
                variant_id=variant_id,
                target_px=target_px,
                requested_metadata_ids=requested,
                sources=(),
                missing_metadata_ids=requested,
                failed_message=f"{type(exc).__name__}: {exc}",
            )
        self._emit_source_batch(result)

    @staticmethod
    def _decode_rgba(path: Path, target_px: int) -> bytes | None:
        try:
            with Image.open(path) as image:
                image.load()
                image.thumbnail((target_px, target_px), Image.Resampling.LANCZOS)
                if image.mode != "RGBA":
                    image = image.convert("RGBA")
                width, height = image.size
                rgba = image.tobytes("raw", "RGBA")
                return width.to_bytes(2, "little") + height.to_bytes(2, "little") + rgba
        except Exception:
            return None

    @staticmethod
    def _decode_cached_payload(
        *,
        metadata_id: int,
        target_px: int,
        variant_id: str | None,
        raw: bytes | None,
    ) -> DecodedThumbnail | None:
        if not raw or len(raw) < 4:
            return None
        width = int.from_bytes(raw[0:2], "little")
        height = int.from_bytes(raw[2:4], "little")
        if width <= 0 or height <= 0:
            return None
        rgba = raw[4:]
        if len(rgba) != width * height * 4:
            return None
        return DecodedThumbnail(
            metadata_id=metadata_id,
            target_px=target_px,
            width=width,
            height=height,
            rgba=rgba,
            variant_id=variant_id,
        )

    def _emit_thumbnail_ready(
        self,
        payload: DecodedThumbnail | ThumbnailDecodeFailure | None,
    ) -> None:
        for callback in tuple(self._callbacks):
            callback(payload)

    def _emit_source_batch(self, payload: ThumbnailSourceBatch) -> None:
        for callback in tuple(self._source_callbacks):
            callback(payload)
