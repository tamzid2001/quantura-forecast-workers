# Retained from forecasting-tools 0.3.4 (MIT); see LICENSE and provenance.json.
from __future__ import annotations
from datetime import datetime
from typing import TypeVar
import pendulum
from pydantic import BaseModel

B = TypeVar("B", bound=BaseModel)

def add_timezone_to_dates_in_base_model(base_model: B) -> B:
    for field_name in type(base_model).model_fields.keys():
        value = getattr(base_model, field_name)
        if isinstance(value, datetime):
            if value is None:
                date = None
            elif value.tzinfo is None:
                date = value.replace(tzinfo=pendulum.timezone("UTC"))
            else:
                date = value
            setattr(base_model, field_name, date)
    return base_model

def clean_indents(text: str) -> str:
    """
    Cleans indents from the text, optimized for prompts
    Note, this is not the same as textwrap.dedent (see the test for this function for examples)
    """
    lines = text.split("\n")
    try:
        indent_level_of_first_line = find_indent_level_of_string(lines[0])
        indent_level_of_second_line = find_indent_level_of_string(lines[1])
        greatest_indent_level_of_first_two_lines = max(
            indent_level_of_first_line, indent_level_of_second_line
        )
    except IndexError:
        greatest_indent_level_of_first_two_lines = find_indent_level_of_string(lines[0])

    new_lines = []
    for line in lines:
        indent_level_of_line = find_indent_level_of_string(line)
        if indent_level_of_line >= greatest_indent_level_of_first_two_lines:
            new_line = line[greatest_indent_level_of_first_two_lines:]
        else:
            new_line = line.lstrip()
        new_lines.append(new_line)

    combined_new_lines = "\n".join(new_lines)
    return combined_new_lines

def find_indent_level_of_string(string: str) -> int:
    return len(string) - len(string.lstrip())
