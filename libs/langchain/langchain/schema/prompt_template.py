from __future__ import annotations

import inspect
import json
from abc import ABC, abstractmethod
from functools import partial
from operator import attrgetter
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, TypeAlias, Union, cast

import yaml
from pydantic import Field, root_validator

from langchain.load.serializable import Serializable
from langchain.schema.document import Document
from langchain.schema.output_parser import BaseOutputParser
from langchain.schema.prompt import PromptValue
from langchain.schema.runnable import Runnable, RunnableConfig

BoundFormatValueCallable: TypeAlias = Callable[[Any], Any]

FormatterType: TypeAlias = Union[
    Callable[[Any], Any], Callable[[Any, BoundFormatValueCallable], Any]
]

FormattersType: TypeAlias = Mapping[type, FormatterType]

PROMPT_DEFAULT_FORMATTERS: FormattersType = {
    Document: attrgetter("page_content"),
    list: lambda list_value, format: [format(e) for e in list_value],
    dict: lambda dict_value, format: {k: format(v) for k, v in dict_value.items()},
}


def _apply_formatter(
    formatters: FormattersType, formatter: FormatterType, value: Any
) -> Any:
    try:
        arity = len(inspect.signature(formatter).parameters)
    except ValueError:
        arity = 1
    if arity == 1:
        return cast(Callable[[Any], Any], formatter)(value)
    elif arity == 2:
        return cast(Callable[[Any, BoundFormatValueCallable], Any], formatter)(
            value, partial(_format_value, formatters)
        )
    else:
        raise ValueError(
            f"Formatter {formatter} has too many arguments ({arity}), "
            "expected 1 or 2."
        )


def _format_value(formatters: FormattersType, value: Any) -> Any:
    value_type = type(value)
    # First check for exact type match
    if value_type in formatters:
        return _apply_formatter(formatters, formatters[value_type], value)
    # Then check for subclass match
    for type_, formatter in formatters.items():
        if isinstance(value, type_):
            return _apply_formatter(formatters, formatter, value)
    return value


class BasePromptTemplate(Serializable, Runnable[Dict, PromptValue], ABC):
    """Base class for all prompt templates, returning a prompt."""

    formatters: FormattersType = PROMPT_DEFAULT_FORMATTERS
    """A mapping of types to functions that format them into a string.
    The functions should take a single argument, the value to format, and
    return a string. If the function takes two arguments, the second argument
    will be a function that can be used to format values within the value
    being formatted, eg. the elements of a list.
    
    By default, the following types are supported:
    - `Document`: the `page_content` attribute of the document will be used.
    - `list`: each element of the list will be formatted.
    - `dict`: each value of the dict will be formatted."""
    input_variables: List[str]
    """A list of the names of the variables the prompt template expects."""
    output_parser: Optional[BaseOutputParser] = None
    """How to parse the output of calling an LLM on this formatted prompt."""
    partial_variables: Mapping[str, Union[str, Callable[[], str]]] = Field(
        default_factory=dict
    )

    @property
    def lc_serializable(self) -> bool:
        return True

    class Config:
        """Configuration for this pydantic object."""

        arbitrary_types_allowed = True

    def invoke(self, input: Dict, config: RunnableConfig | None = None) -> PromptValue:
        return self._call_with_config(
            lambda inner_input: self.format_prompt(**inner_input),
            input,
            config,
            run_type="prompt",
        )

    @abstractmethod
    def format_prompt(self, **kwargs: Any) -> PromptValue:
        """Create Chat Messages."""

    @root_validator()
    def validate_variable_names(cls, values: Dict) -> Dict:
        """Validate variable names do not include restricted names."""
        if "stop" in values["input_variables"]:
            raise ValueError(
                "Cannot have an input variable named 'stop', as it is used internally,"
                " please rename."
            )
        if "stop" in values["partial_variables"]:
            raise ValueError(
                "Cannot have an partial variable named 'stop', as it is used "
                "internally, please rename."
            )

        overall = set(values["input_variables"]).intersection(
            values["partial_variables"]
        )
        if overall:
            raise ValueError(
                f"Found overlapping input and partial variables: {overall}"
            )
        return values

    def partial(self, **kwargs: Union[str, Callable[[], str]]) -> BasePromptTemplate:
        """Return a partial of the prompt template."""
        prompt_dict = self.__dict__.copy()
        prompt_dict["input_variables"] = list(
            set(self.input_variables).difference(kwargs)
        )
        prompt_dict["partial_variables"] = {**self.partial_variables, **kwargs}
        return type(self)(**prompt_dict)

    def _prepare_variables(self, **kwargs: Any) -> Dict[str, Any]:
        # Get partial params:
        partial_kwargs = {
            k: v if isinstance(v, str) else v()
            for k, v in self.partial_variables.items()
        }
        all_variables = {**partial_kwargs, **kwargs}
        return {k: _format_value(self.formatters, v) for k, v in all_variables.items()}

    @abstractmethod
    def format(self, **kwargs: Any) -> str:
        """Format the prompt with the inputs.

        Args:
            kwargs: Any arguments to be passed to the prompt template.

        Returns:
            A formatted string.

        Example:

        .. code-block:: python

            prompt.format(variable1="foo")
        """

    @property
    def _prompt_type(self) -> str:
        """Return the prompt type key."""
        raise NotImplementedError

    def dict(self, **kwargs: Any) -> Dict:
        """Return dictionary representation of prompt."""
        prompt_dict = super().dict(**kwargs)
        del prompt_dict["formatters"]
        prompt_dict["_type"] = self._prompt_type
        return prompt_dict

    def save(self, file_path: Union[Path, str]) -> None:
        """Save the prompt.

        Args:
            file_path: Path to directory to save prompt to.

        Example:
        .. code-block:: python

            prompt.save(file_path="path/prompt.yaml")
        """
        if self.partial_variables:
            raise ValueError("Cannot save prompt with partial variables.")
        # Convert file to Path object.
        if isinstance(file_path, str):
            save_path = Path(file_path)
        else:
            save_path = file_path

        directory_path = save_path.parent
        directory_path.mkdir(parents=True, exist_ok=True)

        # Fetch dictionary to save
        prompt_dict = self.dict()

        if save_path.suffix == ".json":
            with open(file_path, "w") as f:
                json.dump(prompt_dict, f, indent=4)
        elif save_path.suffix == ".yaml":
            with open(file_path, "w") as f:
                yaml.dump(prompt_dict, f, default_flow_style=False)
        else:
            raise ValueError(f"{save_path} must be json or yaml")


def format_document(doc: Document, prompt: BasePromptTemplate) -> str:
    """Format a document into a string based on a prompt template.

    First, this pulls information from the document from two sources:

    1. `page_content`:
        This takes the information from the `document.page_content`
        and assigns it to a variable named `page_content`.
    2. metadata:
        This takes information from `document.metadata` and assigns
        it to variables of the same name.

    Those variables are then passed into the `prompt` to produce a formatted string.

    Args:
        doc: Document, the page_content and metadata will be used to create
            the final string.
        prompt: BasePromptTemplate, will be used to format the page_content
            and metadata into the final string.

    Returns:
        string of the document formatted.

    Example:
        .. code-block:: python

            from langchain.schema import Document
            from langchain.prompts import PromptTemplate
            doc = Document(page_content="This is a joke", metadata={"page": "1"})
            prompt = PromptTemplate.from_template("Page {page}: {page_content}")
            format_document(doc, prompt)
            >>> "Page 1: This is a joke"
    """
    base_info = {"page_content": doc.page_content, **doc.metadata}
    missing_metadata = set(prompt.input_variables).difference(base_info)
    if len(missing_metadata) > 0:
        required_metadata = [
            iv for iv in prompt.input_variables if iv != "page_content"
        ]
        raise ValueError(
            f"Document prompt requires documents to have metadata variables: "
            f"{required_metadata}. Received document with missing metadata: "
            f"{list(missing_metadata)}."
        )
    document_info = {k: base_info[k] for k in prompt.input_variables}
    return prompt.format(**document_info)
