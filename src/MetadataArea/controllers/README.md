# Metadata controllers

The main controller owns the table and coordinates Custom Groups. Import/export
dialogs have their own controllers and Designer forms.

Controllers bind widgets and call services. Keep matching and database writes
outside this folder. Send change notifications only after successful commits so
other workspaces refresh durable data. See the [area guide](../README.md).
