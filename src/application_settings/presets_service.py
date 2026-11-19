"""Project-scoped preset browsing and checked bulk deletion."""
from application_settings.presets_repository import ProjectPresetsRepository


class PresetsService:
    def __init__(self, projects):
        self.projects = projects

    def list_projects(self, current=None):
        projects = self.projects.list_known_projects()
        if current is not None:
            projects = [current, *(project for project in projects if project.root != current.root)]
        return projects

    def inspect_project(self, path):
        return self.projects.inspect_project(path)

    def list_groups(self, project):
        self.projects.inspect_project(project.root)
        return ProjectPresetsRepository(project).list_groups()

    def delete(self, project, paths):
        self.projects.inspect_project(project.root)
        repository = ProjectPresetsRepository(project)
        paths = repository.validate_paths(paths)
        references = repository.required_references()
        blocked = [f"{path.name}: used by {', '.join(sorted(references[path.resolve()]))}"
                   for path in paths if path.resolve() in references]
        if blocked:
            raise ValueError("No presets were deleted. These presets are still required:\n\n" + "\n".join(blocked))
        return repository.delete_paths(paths)
