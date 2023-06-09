from __future__ import annotations
from collections.abc import Mapping, Sequence
from typing import Any, Optional
import json

from pydantic import schema_of, parse_obj_as, ValidationError
from .w3c_td_model import DataSchema


JSONSchema = dict[str, Any]  # A type to represent JSONSchema


def is_a_reference(d: JSONSchema) -> bool:
    """Return True if a JSONSchema dict is a reference
    
    JSON Schema references are one-element dictionaries with
    a single key, `$ref`.  `pydantic` sometimes breaks this
    rule and so I don't check that it's a single key.
    """
    return "$ref" in d


def look_up_reference(reference: str, d: JSONSchema) -> JSONSchema:
    """Look up a reference in a JSONSchema
    
    This first asserts the reference is local (i.e. starts with #
    so it's relative to the current file), then looks up
    each path component in turn.
    """
    if not reference.startswith("#/"):
        raise NotImplementedError(
            "Built-in resolver can only dereference internal JSON references (i.e. starting with #)."
        )
    try:
        resolved: JSONSchema = d
        for key in reference[2:].split("/"):
            resolved = resolved[key]
        return resolved
    except KeyError as ke:
        raise KeyError(f"The JSON reference {reference} was not found in the schema (original error {ke}).")
    
def is_an_object(d: JSONSchema) -> bool:
    """Determine whether a JSON schema dict is an object"""
    return "type" in d and d["type"] == "object"


def convert_object(d: JSONSchema) -> JSONSchema:
    """Convert an object from JSONSchema to Thing Description"""
    out: JSONSchema = d.copy()
    # AdditionalProperties is not supported by Thing Description, and it is ambiguous
    # whether this implies it's false or absent. I will, for now, ignore it, so we
    # delete the key below.
    if "additionalProperties" in out:
        del out["additionalProperties"]
    return out
    


def check_recursion(depth: int, limit: int):
    """Check the recursion count is less than the limit"""
    if depth > limit:
        raise ValueError(
            f"Recursion depth of {limit} exceeded - perhaps there is a circular reference?"
        )


def jsonschema_to_dataschema(
        d: JSONSchema, 
        root_schema: Optional[JSONSchema] = None,
        recursion_depth: int = 0,
        recursion_limit: int = 99,
    ) -> JSONSchema:
    """remove references and change field formats
    
    JSONSchema allows schemas to be replaced with `{"$ref": "#/path/to/schema"}`.
    Thing Description does not allow this. `dereference_jsonschema_dict` takes a
    `dict` representation of a JSON Schema document, and replaces all the
    references with the appropriate chunk of the file.

    JSONSchema can represent `Union` types using the `anyOf` keyword, which is
    not supported by Thing Description.  It's possible to achieve the same thing
    in the specific case of array elements, by setting `items` to a list of
    `DataSchema` objects. This function does not yet do that conversion.
    
    This generates a copy of the document, to avoid messing up `pydantic`'s cache.
    """
    root_schema = root_schema or d
    check_recursion(recursion_depth, recursion_limit)
    # JSONSchema references are one-element dictionaries, with a single key called $ref
    while is_a_reference(d):
        d = look_up_reference(d["$ref"], root_schema)
        recursion_depth += 1
        check_recursion(recursion_depth, recursion_limit)
    
    if is_an_object(d):
        d = convert_object(d)
    
    # TODO: convert anyOf to an array, where possible

    # After checking the object isn't a reference, we now recursively check sub-dictionaries
    # and dereference those if necessary. This could be done with a comprehension, but I
    # am prioritising readability over speed. This code is run when generating the TD, not
    # in time-critical situations.
    rkwargs = {
        "root_schema": root_schema,
        "recursion_depth": recursion_depth+1,
        "recursion_limit": recursion_limit,
    }
    output: JSONSchema = {}
    for k, v in d.items():
        if isinstance(v, Mapping):
            # Any items that are Mappings (i.e. sub-dictionaries) must be recursed into
            output[k] = jsonschema_to_dataschema(v, **rkwargs)
        elif isinstance(v, Sequence) and len(v) > 0 and isinstance(v[0], Mapping):
            # We can also have lists of mappings (i.e. Array[DataSchema]), so we
            # recurse into these.
            output[k] = [jsonschema_to_dataschema(item, **rkwargs) for item in v]
        else:
            output[k] = v
    return output


def type_to_dataschema(t: type, **kwargs) -> DataSchema:
    """Convert a Python type to a Thing Description DataSchema
    
    This makes use of pydantic's `schema_of` function to create a
    json schema, then applies some fixes to make a DataSchema
    as per the Thing Description (because Thing Description is
    almost but not quite compatible with JSONSchema).

    Additional keyword arguments are added to the DataSchema,
    and will override the fields generated from the type that
    is passed in. Typically you'll want to use this for the 
    `title` field.
    """
    schema_dict = jsonschema_to_dataschema(schema_of(t))
    # Definitions of referenced ($ref) schemas are put in a
    # key called "definitions" by pydantic. We should delete this.
    # TODO: find a cleaner way to do this
    # This shouldn't be a severe problem: we will fail with a
    # validation error if other junk is left in the schema.
    if "definitions" in schema_dict:
        del schema_dict["definitions"]
    schema_dict.update(kwargs)
    try:
        return parse_obj_as(DataSchema, schema_dict)
    except ValidationError as ve:
        print(
            "Error while constructing DataSchema from the "
            "following dictionary:\n" +
            json.dumps(schema_dict, indent=2)
        )
        raise ve