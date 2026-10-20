"""Terminate isolated sklearn work, never a Qt worker thread."""
from __future__ import annotations
import multiprocessing


class ClusteringCancelled(Exception):
    pass


def _child(sender, project, record, directory):
    try:
        from EmbeddingsArea.clustering.pipeline import compute
        result = compute(project, record, directory, lambda value: sender.send(("status", value)))
        sender.send(("result", result))
    except BaseException as exc:
        sender.send(("error", f"{type(exc).__name__}: {exc}"))
    finally:
        sender.close()


def run_isolated(project, record, directory, cancelled=lambda: False, status=lambda value: None):
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=_child, args=(sender, project, record, str(directory)), name="eidora-clustering", daemon=True)
    process.start()
    sender.close()
    try:
        while True:
            if cancelled():
                raise ClusteringCancelled("Clustering aborted.")
            if receiver.poll(.1):
                try:
                    kind, value = receiver.recv()
                except EOFError as exc:
                    raise RuntimeError(f"Clustering process exited unexpectedly ({process.exitcode}).") from exc
                if kind == "result":
                    return value
                if kind == "error":
                    raise RuntimeError(value)
                status(value)
            elif not process.is_alive():
                raise RuntimeError(f"Clustering process exited unexpectedly ({process.exitcode}).")
    finally:
        if process.is_alive():
            process.terminate()
        process.join(3)
        if process.is_alive():
            process.kill()
            process.join()
        receiver.close()


def _probe(sender):
    """Exercise lazy estimator imports in a spawned (also frozen) child."""
    try:
        import numpy as np
        from EmbeddingsArea.clustering.registry import ClusteringRegistry
        values = np.random.default_rng(42).normal(size=(20, 4)).astype(np.float32)
        methods = []
        registry = ClusteringRegistry()
        for descriptor in registry.list_methods():
            registry.require_method(descriptor.id).fit(values, {})
            methods.append(descriptor.id)
        sender.send((True, methods))
    except BaseException as exc:
        sender.send((False, f"{type(exc).__name__}: {exc}"))
    finally:
        sender.close()


def probe_runtime(timeout=30):
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=_probe, args=(sender,), daemon=True)
    process.start()
    sender.close()
    try:
        if not receiver.poll(timeout):
            raise RuntimeError("Clustering subprocess self-test timed out.")
        success, value = receiver.recv()
        if not success:
            raise RuntimeError(value)
        return value
    finally:
        process.join(3)
        if process.is_alive():
            process.kill()
            process.join()
        receiver.close()
