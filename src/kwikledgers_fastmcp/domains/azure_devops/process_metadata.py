from __future__ import annotations

import json
import os

from .configuration import get_client, load_configuration
from .services import _extract_reference_name, _resolve_work_item_type_name


def read_work_item_process_metadata() -> str:
    load_configuration()
    project = os.environ.get("AZURE_PROJECT", "").strip()
    area = os.environ.get("AZURE_PROJECT_AREA", "").strip()
    if not project or not area:
        raise ValueError("Process metadata requires AZURE_PROJECT and AZURE_PROJECT_AREA.")
    wit = get_client().clients.get_work_item_tracking_client()
    definitions = wit.get_fields(project=project)
    if not definitions:
        raise ValueError("Azure returned no project field definitions.")
    fields = {}
    for definition in definitions:
        reference = _extract_reference_name(definition)
        if not reference or type(definition.read_only) is not bool:
            raise ValueError("Azure returned incomplete project field metadata.")
        fields[reference] = definition.read_only
    types = []
    for requested in ("story", "task", "bug"):
        native_type = _resolve_work_item_type_name(wit, requested)
        definitions = wit.get_work_item_type_fields_with_references(project, native_type, expand="All")
        if not definitions:
            raise ValueError(f"Azure returned no field constraints for {native_type}.")
        constraints = []
        for definition in definitions:
            reference = _extract_reference_name(definition)
            if not reference:
                raise ValueError(f"Azure returned incomplete constraints for {native_type}: field reference missing.")
            if type(definition.always_required) is not bool:
                raise ValueError(f"Azure returned incomplete constraints for {native_type}: always_required missing for {reference}.")
            if definition.allowed_values is not None and not isinstance(definition.allowed_values, list):
                raise ValueError(f"Azure returned incomplete constraints for {native_type}: invalid allowed_values for {reference}.")
            constraints.append({
                "reference_name": reference,
                "read_only": fields.get(reference),
                "always_required": definition.always_required,
                "default_value": definition.default_value,
                "allowed_values": definition.allowed_values,
            })
        types.append({"name": native_type, "fields": constraints})
    return json.dumps({"version": 1, "project": project, "area": area, "work_item_types": types},
                      ensure_ascii=True, allow_nan=False)
