"""Cancellable isolated PCA execution, including frozen applications."""
import multiprocessing
from EmbeddingsArea.reduction.parameters import ReductionCancelled


def _child(sender, path, directory, request, source_id, source_label):
    try:
        from EmbeddingsArea.reduction.pipeline import compute
        result = compute(path, directory, request, source_id, lambda message: sender.send(("status", message)), source_label=source_label)
        sender.send(("result", result))
    except BaseException as exc:
        sender.send(("error", f"{type(exc).__name__}: {exc}"))
    finally:
        sender.close()


def run_isolated(path, directory, request, source_id, cancelled, status, *, source_label=""):
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=_child, args=(sender, str(path), str(directory), request, source_id, source_label),
                              name="eidora-reduction", daemon=True)
    try:
        process.start()
        sender.close()
        while True:
            if cancelled():
                raise ReductionCancelled("Reduction cancelled; original vectors are unchanged.")
            if receiver.poll(.1):
                try:
                    kind, value = receiver.recv()
                except EOFError as exc:
                    raise RuntimeError(f"Reduction process exited unexpectedly ({process.exitcode}).") from exc
                if kind == "result":
                    return value
                if kind == "error":
                    raise RuntimeError(value)
                status(value)
            elif not process.is_alive():
                raise RuntimeError(f"Reduction process exited unexpectedly ({process.exitcode}).")
    finally:
        if process.pid is not None:
            process.join(.2)
            if process.is_alive():
                process.terminate()
                process.join(3)
            if process.is_alive():
                process.kill()
                process.join()
        receiver.close()
        sender.close()
