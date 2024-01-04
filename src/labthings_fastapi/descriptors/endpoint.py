from __future__ import annotations
from functools import partial

from labthings_fastapi.utilities.introspection import get_docstring, get_summary

from typing import (
    Callable,
    Literal,
    Mapping,
    Optional,
    Self,
    Union,
    overload,
    TYPE_CHECKING,
)
from fastapi import FastAPI

if TYPE_CHECKING:
    from ..thing import Thing

HTTPMethod = Literal["get", "post", "put", "delete"]


class EndpointDescriptor:
    """A descriptor to allow Things to easily add other endpoints"""

    def __init__(
        self,
        func: Callable,
        http_method: HTTPMethod = "get",
        path: Optional[str] = None,
        **kwargs: Mapping,
    ):
        self.func = func
        self.http_method = http_method
        self._path = path
        self.kwargs = kwargs

    @overload
    def __get__(self, obj: Literal[None], type=None) -> Self:
        ...

    @overload
    def __get__(self, obj: Thing, type=None) -> Callable:
        ...

    def __get__(self, obj: Optional[Thing], type=None) -> Union[Self, Callable]:
        """The function, bound to an object as for a normal method.

        If `obj` is None, the descriptor is returned, so we can get
        the descriptor conveniently as an attribute of the class.
        """
        if obj is None:
            return self
        # TODO: do we attempt dependency injection here? I think not.
        # If we want dependency injection, we should be calling the action
        # via some sort of client object.
        return partial(self.func, obj)

    @property
    def name(self):
        """The name of the wrapped function"""
        return self.func.__name__

    @property
    def path(self):
        """The path of the endpoint (relative to the Thing)"""
        return self._path or self.name

    @property
    def title(self):
        """A human-readable title"""
        return get_summary(self.func) or self.name

    @property
    def description(self):
        """A description of the action"""
        return get_docstring(self.func, remove_summary=True)

    def add_to_fastapi(self, app: FastAPI, thing: Thing):
        """Add this function to a FastAPI app, bound to a particular Thing."""
        # fastapi_endpoint is equivalent to app.get/app.post/whatever
        fastapi_endpoint = getattr(app, self.http_method)
        bound_function = self.__get__(thing)
        fastapi_endpoint(thing.path + self.path, **self.kwargs)(bound_function)
