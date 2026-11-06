"""Identify saved settings errors without discarding the user's document."""

from copy import deepcopy

from pydantic import ValidationError

from .models import AppConfig, Profile


def setting_value(document, path):
    for key in path:
        document = document[key]
    return document


def validation_issues(error, document, *, profile_id=None, preview=None):
    issues = []
    for item in error.errors(include_url=False, include_context=False, include_input=False):
        path = list(item["loc"])
        selected_id = profile_id
        if len(path) > 1 and path[0] == "profiles" and isinstance(path[1], int):
            try:
                selected_id = document["profiles"][path[1]].get("id")
            except (KeyError, IndexError, TypeError, AttributeError):
                pass
            if preview is not None:
                selected_id = preview.profiles[path[1]].id if path[1] < len(preview.profiles) else None
        try:
            value = setting_value(document, path)
        except (KeyError, IndexError, TypeError):
            value = None
        issues.append({"path": path, "field": ".".join(map(str, path)),
                       "message": item["msg"], "kind": item["type"], "profileId": selected_id, "value": value})
    return issues


def repair_preview(document):
    """Build a valid display model; the original document stays authoritative."""
    candidate = deepcopy(document)
    defaults = AppConfig().model_dump()
    for _ in range(50):
        try:
            return AppConfig.model_validate(candidate)
        except ValidationError as error:
            for issue in error.errors(include_url=False):
                path = list(issue["loc"])
                template = defaults
                profile_defaults = None
                if len(path) > 1 and path[0] == "profiles" and isinstance(path[1], int):
                    index = path[1]
                    identity = {"id": f"repair-profile-{index}", "name": "Profile needing repair"}
                    try:
                        raw = document["profiles"][index]
                        identity = Profile(id=raw.get("id", identity["id"]),
                                           name=raw.get("name") if isinstance(raw.get("name"), str) else identity["name"]).model_dump()
                    except (AttributeError, KeyError, TypeError, ValidationError):
                        pass
                    profile_defaults = Profile.model_validate(identity).model_dump()
                    template = {"profiles": {path[1]: profile_defaults}}
                if not path:
                    candidate = deepcopy(defaults)
                    break
                try:
                    replacement = deepcopy(setting_value(template, path))
                    parent = setting_value(candidate, path[:-1])
                    parent[path[-1]] = replacement
                except (KeyError, IndexError, TypeError):
                    # A missing/invalid container is replaced as a unit, leaving
                    # its original value available in the repair panel.
                    path = path[:2] if path[0] == "profiles" and len(path) > 1 else path[:1]
                    replacement = deepcopy(profile_defaults if len(path) == 2 else defaults.get(path[0]))
                    setting_value(candidate, path[:-1])[path[-1]] = replacement
    raise ValueError("Could not build a safe settings repair preview")
