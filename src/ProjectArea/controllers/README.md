# Project area controllers

The main controller composes Media Sources and Thumbnails side by side and
publishes narrow change callbacks to the main window. Thumbnails owns its saved
sets, creation dialog, progress, cancellation, and unfinished-job recovery.
Link and Relink share one exact-key preview dialog; the caller fixes whether
existing source links are preserved or replaced. Unsupported saved rules are
never silently translated into the new choices.

Keep scanning, resizing, decoding, persistence, and worker orchestration outside
the area-level controller. Stop active jobs before replacing a project and
reject callbacks from an earlier project generation.
