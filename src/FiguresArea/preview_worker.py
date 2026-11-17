"""Persistent headless figure renderer. Never imports a Qt canvas."""
from __future__ import annotations

import gc

# Keep Connection's internal receive buffers small. Growing a full-page
# BytesIO for each reply can retain large allocations in the GUI process.
PIXEL_CHUNK_BYTES = 256 * 1024


def run(connection) -> None:
    from FiguresArea.renderer import FigureMatplotlibRenderer
    from FiguresArea.point_preview_cache import PointPreviewCache
    from FiguresArea.repository import FigureTabsRepository
    from FiguresArea.preview_transfer import PreviewTransfer
    import numpy as np

    points = PointPreviewCache()
    renderer = FigureMatplotlibRenderer(point_cache=points, reuse_page=True)
    transfer = PreviewTransfer()
    try:
        while True:
            request = connection.recv()
            if request is None:
                break
            result = composition = cell = canvas = pixels = reply = region_pixels = None
            regions = None
            try:
                if request.get('measurements') is not None:
                    from FiguresArea.measurements import prepare_measurements
                    reply = prepare_measurements(request['measurements'], request['measurement_values'], request.get('selected_cell'))
                    connection.send((True, reply))
                    continue
                if request.get("load"):
                    from pathlib import Path
                    reply = FigureTabsRepository(Path(request["load"])).load_compact()
                    connection.send((True, reply))
                    continue
                composition = FigureTabsRepository.composition_from_payload(
                    request["composition"], settings=request["settings"],
                )
                if composition is None:
                    raise ValueError("Missing figure composition")
                for cell in composition.plot_cells.values():
                    cell.data_key = request.get("data_keys", {}).get((cell.row, cell.column), "")
                if request.get("export"):
                    from pathlib import Path
                    FigureMatplotlibRenderer().export(composition, Path(request["export"]))
                    reply = {"export": request["export"]}
                else:
                    result = renderer.build(composition)
                    canvas = result.figure.canvas
                    draw_stats = renderer.draw_preview(result)
                    pixels = canvas.buffer_rgba()
                    reply = {"layout": result.layout_map, "pixel_bytes": pixels.nbytes,
                             "pixel_chunk_bytes": PIXEL_CHUNK_BYTES,
                             "size": (pixels.shape[1], pixels.shape[0]),
                             "cache_hits": points.hits, "cache_misses": points.misses,
                             "draw_stats": draw_stats}
                    if request.get("incremental"):
                        regions = transfer.prepare(np.asarray(pixels), request.get("base_frame"))
                        reply.update(frame_id=transfer.frame_id)
                        if regions is not None:
                            reply.update(regions=regions, base_frame=request.get("base_frame"),
                                         pixel_bytes=sum(w*h*4 for _, _, w, h in regions))
                    else:
                        transfer = PreviewTransfer()
                connection.send((True, reply))
                if pixels is not None:
                    if regions is None:
                        for offset in range(0, pixels.nbytes, PIXEL_CHUNK_BYTES):
                            connection.send_bytes(pixels.cast('B')[offset:offset + PIXEL_CHUNK_BYTES])
                    else:
                        for x, y, width, height in regions:
                            region_pixels = np.asarray(pixels)[y:y+height, x:x+width].tobytes()
                            for offset in range(0, len(region_pixels), PIXEL_CHUNK_BYTES):
                                connection.send_bytes(region_pixels[offset:offset + PIXEL_CHUNK_BYTES])
            except Exception as error:
                renderer.discard_preview()
                transfer = PreviewTransfer()
                connection.send((False, f"{type(error).__name__}: {error}"))
            finally:
                renderer.finish_preview()
                renderer._data_cache.trim()
                # Retain only the current page and bounded caches, not its
                # decoded composition or per-request pixel views.
                request = result = composition = cell = canvas = pixels = reply = region_pixels = None
                # Retired artist cycles can retain data even after clear().
                # Collect only in this isolated worker, after
                # sending the reply and dropping every per-request reference.
                # Live point/data caches remain reusable across requests.
                gc.collect()
    except (EOFError, BrokenPipeError):
        pass
    finally:
        renderer.discard_preview()
        connection.close()
