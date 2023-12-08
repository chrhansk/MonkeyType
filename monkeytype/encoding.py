# Copyright (c) 2017-present, Facebook, Inc.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.
import json
import logging
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple, Type, TypeVar

from mypy_extensions import TypedDict

from monkeytype.compat import is_any, is_generic, is_union, qualname_of_generic
from monkeytype.db.base import CallTraceStore, CallTraceThunk
from monkeytype.exceptions import InvalidTypeError
from monkeytype.stubs import FunctionKind, get_updated_definition
from monkeytype.tracing import CallTrace
from monkeytype.typing import NoneType, NotImplementedType, is_typed_dict, mappingproxy
from monkeytype.util import get_func_in_module, get_name_in_module

logger = logging.getLogger(__name__)


# Types are converted to dictionaries of the following form before
# being JSON encoded and sent to storage:
#
#     {
#         'module': '<module>',
#         'qualname': '<qualname>',
#         'elem_types': [type_dict],
#     }
#
# The corresponding type alias should actually be
#
#     TypeDict = Dict[str, Union[str, TypeDict]]
#
# (or better, a TypedDict) but mypy does not support recursive type aliases:
#  https://github.com/python/mypy/issues/731
TypeDict = Dict[str, Any]


def typed_dict_to_dict(typ: type) -> TypeDict:
    elem_types: Dict[str, Any] = {}
    for k, v in typ.__annotations__.items():
        elem_types[k] = type_to_dict(v)
    return {
        "module": typ.__module__,
        "qualname": typ.__qualname__,
        "elem_types": elem_types,
        "is_typed_dict": True,
    }


def type_to_dict(typ: type) -> TypeDict:
    """Convert a type into a dictionary representation that we can store.

    The dictionary must:
        1. Be encodable as JSON
        2. Contain enough information to let us reify the type
    """
    if is_typed_dict(typ):
        return typed_dict_to_dict(typ)

    # Union and Any are special cases that aren't actually types.
    if is_union(typ):
        qualname = "Union"
    elif is_any(typ):
        qualname = "Any"
    elif is_generic(typ):
        qualname = qualname_of_generic(typ)
    else:
        qualname = typ.__qualname__
    d: TypeDict = {
        "module": typ.__module__,
        "qualname": qualname,
    }
    elem_types = getattr(typ, "__args__", None)
    # In Python < 3.9, bare generics still have args
    is_bare_generic = typ in {Dict, List, Tuple}
    if not is_bare_generic and elem_types is not None and is_generic(typ):
        # empty typing.Tuple is weird; the spec says it should be Tuple[()],
        # which results in __args__ of `((),)` pre-Python 3.11
        if elem_types == ((),):
            elem_types = ()
        d["elem_types"] = [type_to_dict(t) for t in elem_types]
    return d


_HIDDEN_BUILTIN_TYPES: Dict[str, type] = {
    # Types that are inaccessible by their names in the builtins module.
    "NoneType": NoneType,
    "NotImplementedType": NotImplementedType,
    "mappingproxy": mappingproxy,
}


def typed_dict_from_dict(d: TypeDict, store: CallTraceStore) -> type:
    return TypedDict(
        d["qualname"], {k: type_from_dict(v, store) for k, v in d["elem_types"].items()}
    )


builtin_types = {
    "NoneType": NoneType,
    "NotImplementedType": NotImplementedType,
    "mappingproxy": mappingproxy,
}


def get_builtin_special_type(module: str, qualname: str) -> Optional[type]:
    if module == "builtins":
        return builtin_types.get(qualname, None)
    return None


def get_function_annotation(d: TypeDict, typ: type, store: CallTraceStore) -> Any:
    thunks = store.filter(d["module"], d["qualname"])
    traces: List[CallTrace] = [thunk.to_trace(store) for thunk in thunks]

    func_def = get_updated_definition(typ, traces, 10)

    ret_val = func_def.signature.return_annotation
    params = list(func_def.signature.parameters.values())

    # First param is self for instance methods
    if func_def.kind == FunctionKind.INSTANCE:
        params = params[1:]

    annotations = [param.annotation for param in params]

    return Callable[annotations, ret_val]


def type_from_dict(d: TypeDict, store: CallTraceStore) -> Any:
    """Given a dictionary produced by type_to_dict, return the equivalent type.

    Raises:
        NameLookupError if we can't reify the specified type
        InvalidTypeError if the named type isn't actually a type
    """
    module, qualname = d["module"], d["qualname"]

    if d.get("is_typed_dict", False):
        return typed_dict_from_dict(d, store)

    typ = get_builtin_special_type(module, qualname)

    if typ is not None:
        return typ

    typ = get_name_in_module(module, qualname)

    import types

    if module == "builtins" and qualname in _HIDDEN_BUILTIN_TYPES:
        typ = _HIDDEN_BUILTIN_TYPES[qualname]
    elif isinstance(typ, types.FunctionType):
        return get_function_annotation(d, typ, store)
    if not (isinstance(typ, type) or is_any(typ) or is_generic(typ)):
        raise InvalidTypeError(
            f"Attribute specified by '{qualname}' in module '{module}' "
            f"is of type {type(typ)}, not type."
        )
    elem_type_dicts = d.get("elem_types")
    if elem_type_dicts is not None and is_generic(typ):
        elem_types = tuple(type_from_dict(e, store) for e in elem_type_dicts)
        # mypy complains that a value of type `type` isn't indexable. That's
        # true, but we know typ is a subtype that is indexable. Even checking
        # with hasattr(typ, '__getitem__') doesn't help
        typ = typ[elem_types]  # type: ignore[index]
    return typ


def type_to_json(typ: type) -> str:
    """Encode the supplied type as json using type_to_dict."""
    type_dict = type_to_dict(typ)
    return json.dumps(type_dict, sort_keys=True)


def type_from_json(typ_json: str, store: CallTraceStore) -> Any:
    """Reify a type from the format produced by type_to_json."""
    type_dict = json.loads(typ_json)
    return type_from_dict(type_dict, store)


def arg_types_to_json(arg_types: Dict[str, type], store: CallTraceStore) -> str:
    """Encode the supplied argument types as json"""
    type_dict = {name: type_to_dict(typ) for name, typ in arg_types.items()}
    return json.dumps(type_dict, sort_keys=True)


def arg_types_from_json(arg_types_json: str, store: CallTraceStore) -> Dict[str, Any]:
    """Reify the encoded argument types from the format produced by arg_types_to_json."""
    arg_types = json.loads(arg_types_json)
    return {
        name: type_from_dict(type_dict, store) for name, type_dict in arg_types.items()
    }


TypeEncoder = Callable[[type], str]


def maybe_encode_type(encode: TypeEncoder, typ: Optional[type]) -> Optional[str]:
    if typ is None:
        return None
    return encode(typ)


TypeDecoder = Callable[[str, CallTraceStore], type]


def maybe_decode_type(
    decode: TypeDecoder, encoded: Optional[str], store: CallTraceStore
) -> Optional[type]:
    if (encoded is None) or (encoded == "null"):
        return None
    return decode(encoded, store)


CallTraceRowT = TypeVar("CallTraceRowT", bound="CallTraceRow")


class CallTraceRow(CallTraceThunk):
    """A semi-structured call trace where each field has been json encoded."""

    def __init__(
        self,
        module: str,
        qualname: str,
        arg_types: str,
        return_type: Optional[str],
        yield_type: Optional[str],
    ) -> None:
        self.module = module
        self.qualname = qualname
        self.arg_types = arg_types
        self.return_type = return_type
        self.yield_type = yield_type

    @classmethod
    def from_trace(
        cls: Type[CallTraceRowT], trace: CallTrace, store: CallTraceStore
    ) -> CallTraceRowT:
        module = trace.func.__module__
        qualname = trace.func.__qualname__
        arg_types = arg_types_to_json(trace.arg_types, store)
        return_type = maybe_encode_type(type_to_json, trace.return_type)
        yield_type = maybe_encode_type(type_to_json, trace.yield_type)
        return cls(module, qualname, arg_types, return_type, yield_type)

    def to_trace(self, store: CallTraceStore) -> CallTrace:
        function = get_func_in_module(self.module, self.qualname)
        arg_types = arg_types_from_json(self.arg_types, store)
        return_type = maybe_decode_type(type_from_json, self.return_type, store)
        yield_type = maybe_decode_type(type_from_json, self.yield_type, store)
        return CallTrace(function, arg_types, return_type, yield_type)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, CallTraceRow):
            return (
                self.module,
                self.qualname,
                self.arg_types,
                self.return_type,
                self.yield_type,
            ) == (
                other.module,
                other.qualname,
                other.arg_types,
                other.return_type,
                other.yield_type,
            )
        return NotImplemented


def serialize_traces(
    traces: Iterable[CallTrace], store: CallTraceStore
) -> Iterable[CallTraceRow]:
    """Serialize an iterable of CallTraces to an iterable of CallTraceRow.

    Catches and logs exceptions, so a failure to serialize one CallTrace doesn't
    lose all traces.

    """
    for trace in traces:
        try:
            yield CallTraceRow.from_trace(trace, store)
        except Exception:
            logger.exception("Failed to serialize trace")
