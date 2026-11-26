# Embedding controllers

`main_controller.py` composes Models and Vector Data. Model-row Run actions
open the fixed-model execution dialog; Vector Data owns results and imports.
`model_install_coordinator.py` owns downloads independently of that dialog.
`execution_coordinator.py` owns embedding and clustering jobs independently of
the panels. `vector_data_panel_controller.py` joins run and dataset records by
run ID, while `vector_data_card_controller.py` updates each card and its nested
clustering tree in place.

Saved and queued embedding cards start collapsed. When an embedding begins
running, the panel collapses the other cards and opens that run's Progress tab.
Refreshes, progress updates and completion preserve the user's expansion choice;
the last run stays open until closed or another run starts.

Opening a card or changing its details tab can populate the clustering tree.
Resolve selection by clustering ID after those signals return. Detail refreshes
insert replacement rows before retiring old ones to keep accessibility models
populated throughout the update.
Published groups notify dependent areas through the execution coordinator's
group-change signal, independently of result-card selection.

Services own workflow decisions. Workers call services/computation and return
plain results. Cancellation must stop child processes cooperatively, never kill
a Qt thread. Reject stale callbacks after a project changes; completion signals
own worker/thread deletion. Model-catalog callbacks also need shutdown guards.

Embedding jobs use a GUI-owned `QThread` runner for the plain Python generation
job. Result signals report the outcome; only `QThread.finished` releases scheduler
admission and permits deletion on the GUI thread. Release the job's signal
callbacks before that signal. Do not move a callback-owning embedding worker
QObject into the background thread and delete it concurrently with UI refresh.

The embedding runner keeps one latest progress value behind a lock. Its GUI-owned
timer publishes at most four updates per second and flushes/stops on thread finish.
Stage messages are delivered immediately and retained in a 500-event history;
ordinary counts/rates do not enter the log. Cards update live labels separately
from the incremental plain-text log, skip hidden reports, and avoid row resizing
for progress-only updates. Full failure details remain copyable beyond the live
log's 1,000-line display limit.

See [Embeddings](../README.md) for persistence and identity contracts.

The model dialog only saves the run configuration. The embedding worker owns input
preparation, progress, pause and cancellation. After successful worker completion,
the coordinator requests optional thumbnails through the main window. Media owns
that separate job and its errors; the current workspace stays open and the footer
tracks the background job. Project changes and shutdown suppress
the handoff. Existing or pending thumbnails suppress duplicate default jobs.
