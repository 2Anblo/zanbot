"""Tools the model can call, and the registry that describes and runs them."""

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Callable

SIDE_EFFECTS = ("none", "local", "external")  # none: read-only; local: changes this computer; external: reaches out
MAX_NOTE_CHARS = 20_000
# Letters (any script), digits, space, dot, dash, underscore; no path separators or drive letters.
NOTE_NAME = re.compile(r"[\w][\w .\-]{0,99}")
WINDOWS_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


class ToolError(Exception):
    """A failure the model should see and can react to, such as a missing file."""


@dataclass(frozen=True)
class ToolContext:
    notes_dir: Path


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict
    side_effect: str
    run: Callable[[dict, ToolContext], str]


_TYPES = {"string": str, "boolean": bool, "object": dict, "array": list}


def validate(value, schema):
    """Check a tool input against the small JSON Schema subset our tools use; return a problem or None."""
    if not isinstance(value, dict):
        return "input must be an object"
    properties = schema.get("properties", {})
    for name in schema.get("required", []):
        if name not in value:
            return f"missing required '{name}'"
    for name, item in value.items():
        if name not in properties:
            if schema.get("additionalProperties") is False:
                return f"unexpected '{name}'"
            continue
        expected = properties[name].get("type")
        if expected == "integer":
            ok = isinstance(item, int) and not isinstance(item, bool)
        else:
            ok = expected not in _TYPES or isinstance(item, _TYPES[expected])
        if not ok:
            return f"'{name}' must be {'an' if expected[0] in 'aeiou' else 'a'} {expected}"
    return None


class ToolRegistry:
    """Describe tools to the API and run calls, turning every failure into a result for the model."""

    def __init__(self, tools, context):
        self.tools = {}
        for tool in tools:
            if tool.name in self.tools:
                raise ValueError(f"Duplicate tool name: {tool.name}")
            if tool.side_effect not in SIDE_EFFECTS:
                raise ValueError(f"Unknown side effect for {tool.name}: {tool.side_effect}")
            self.tools[tool.name] = tool
        self.context = context

    def schemas(self):
        # Sorted, so the request prefix stays identical between turns (good for prompt caching later).
        return [{"name": t.name, "description": t.description, "input_schema": t.input_schema}
                for t in sorted(self.tools.values(), key=lambda t: t.name)]

    def has_side_effect(self, name):
        tool = self.tools.get(name)
        return tool is not None and tool.side_effect != "none"

    def execute(self, call):
        """Return (text, is_error)."""
        tool = self.tools.get(call["name"])
        if tool is None:
            return f"Unknown tool '{call['name']}'. Available: {', '.join(sorted(self.tools))}.", True
        problem = validate(call["input"], tool.input_schema)
        if problem:
            return f"Invalid input for {tool.name}: {problem}.", True
        try:
            return tool.run(call["input"], self.context), False
        except ToolError as exc:
            return str(exc), True
        except OSError as exc:
            return f"{tool.name} failed: {exc.strerror or exc}", True


def _inside_notes(context, relative):
    root = context.notes_dir.resolve()
    target = (root / relative).resolve()
    if not target.is_relative_to(root):
        raise ToolError("The path must be inside the notes folder.")
    return root, target


def list_dir(args, context):
    relative = args.get("path", ".")
    root, target = _inside_notes(context, relative)
    if target == root and not root.exists():
        return "(empty)"
    if not target.is_dir():
        raise ToolError(f"No folder named '{relative}' in the notes folder.")
    names = sorted(p.name + ("/" if p.is_dir() else "") for p in target.iterdir())
    return "\n".join(names) or "(empty)"


def write_note(args, context):
    name, text = args["name"], args["text"]
    # Windows drops trailing dots and spaces, so "a.md." would silently become another file.
    if (not NOTE_NAME.fullmatch(name) or name.rstrip(". ") != name
            or name.split(".")[0].strip().lower() in WINDOWS_RESERVED):
        raise ToolError("Use a plain file name such as 'todo.md': letters, digits, spaces, '.', '-', '_', "
                        "at most 100 characters, no folders.")
    if len(text) > MAX_NOTE_CHARS:
        raise ToolError(f"Notes are limited to {MAX_NOTE_CHARS:,} characters per call.")
    context.notes_dir.mkdir(parents=True, exist_ok=True)
    with open(context.notes_dir / name, "a", encoding="utf-8") as file:
        file.write(text if text.endswith("\n") else text + "\n")
    return f"Appended {len(text)} characters to {name}."


LIST_DIR = Tool(
    "list_dir",
    "List files and folders in the user's Zanbot notes folder. Folder names end with '/'.",
    {"type": "object",
     "properties": {"path": {"type": "string", "description": "Folder inside the notes folder; default '.'."}},
     "additionalProperties": False},
    "none", list_dir)

WRITE_NOTE = Tool(
    "write_note",
    "Append text to a note in the user's Zanbot notes folder, creating the note if needed. "
    "The user is asked for permission first.",
    {"type": "object",
     "properties": {"name": {"type": "string", "description": "File name such as 'todo.md'."},
                    "text": {"type": "string", "description": "Text to append, as a new line."}},
     "required": ["name", "text"], "additionalProperties": False},
    "local", write_note)


def default_tools(notes_dir):
    return ToolRegistry([LIST_DIR, WRITE_NOTE], ToolContext(notes_dir=notes_dir))
