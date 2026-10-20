# Application information

`service.py` parses stable release metadata and selects official installers for
the current platform. `release_client.py` performs asynchronous Qt network lookup;
it does not install or execute downloads. About/help UI lives in MainWindow.

Keep release parsing independent of Qt. Preserve URL/platform validation, request
timeouts, and cancellation when changing lookup behavior. The application version
comes from `eidora_version.py`.
