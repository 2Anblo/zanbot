"""Tools the model can call, and the registry that describes and runs them."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

SIDE_EFFECTS = ("none", "local", "external")  # none: read-only; local: changes this computer; external: reaches out
# none: runs without asking. ask: asks, and the user may answer "always". ask_each_time: asks every time.
PERMISSIONS = ("none", "ask", "ask_each_time")

MAX_READ_BYTES = 5_000_000
DEFAULT_READ_LINES = 400
MAX_WRITE_CHARS = 200_000


class ToolError(Exception):
    """A failure the model should see and can react to, such as a missing file."""


@dataclass(frozen=True)
class ToolContext:
    workspace: Path  # The only folder file tools may touch.
    store: Any = None  # Long-term memory store, or None when memory is off.


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict
    side_effect: str
    run: Callable[[dict, ToolContext], str]
    permission: str = field(default="")  # Empty: "none" for read-only tools, otherwise "ask".

    def __post_init__(self):
        if not self.permission:
            object.__setattr__(self, "permission", "none" if self.side_effect == "none" else "ask")


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
        if "enum" in properties[name] and item not in properties[name]["enum"]:
            return f"'{name}' must be one of {', '.join(properties[name]['enum'])}"
    return None


class ToolRegistry:
    """Describe tools to the API and run calls, turning every failure into a result for the model."""

    def __init__(self, tools, context):
        self.tools = {}
        for tool in tools:
            if tool.name in self.tools:
                raise ValueError(f"Duplicate tool name: {tool.name}")
            if tool.side_effect not in SIDE_EFFECTS or tool.permission not in PERMISSIONS:
                raise ValueError(f"Unknown side effect or permission for {tool.name}")
            self.tools[tool.name] = tool
        self.context = context

    def schemas(self):
        # Sorted, so the request prefix stays identical between turns (good for prompt caching later).
        return [{"name": t.name, "description": t.description, "input_schema": t.input_schema}
                for t in sorted(self.tools.values(), key=lambda t: t.name)]

    def has_side_effect(self, name):
        """Whether a call may change something; such calls are reported if their turn fails."""
        tool = self.tools.get(name)
        return tool is not None and tool.side_effect != "none"

    def needs_permission(self, name):
        tool = self.tools.get(name)
        return tool is not None and tool.permission != "none"

    def allows_always(self, name):
        """Whether the user may answer "always" for this tool, rather than being asked every time."""
        tool = self.tools.get(name)
        return tool is not None and tool.permission == "ask"

    def execute(self, call):
        """Return (text, is_error)."""
        tool = self.tools.get(call["name"])
        if tool is None:
            return f"Unknown tool '{call['name']}'. Available: {', '.join(sorted(self.tools))}.", True
        problem = validate(call["input"], tool.input_schema)
        if problem:
            return f"Invalid input for {tool.name}: {problem}.", True
        try:
            result = tool.run(call["input"], self.context)
        except ToolError as exc:
            return str(exc), True
        except OSError as exc:
            return f"{tool.name} failed: {exc.strerror or exc}", True
        # A tool may return (text, is_error) to report a failure that still has output, such as a command's.
        return result if isinstance(result, tuple) else (result, False)


# --- Files -------------------------------------------------------------------------------------


def resolve(context, path):
    """Return (workspace, target) for a path given by the model, refusing anything outside the workspace."""
    root = context.workspace.resolve()
    # resolve() follows symbolic links, so a link pointing outside is caught too.
    target = (root / path).resolve()
    if not target.is_relative_to(root):
        raise ToolError(f"The path must be inside the workspace ({root}).")
    return root, target


def shown(root, target):
    return target.relative_to(root).as_posix() or "."


def list_dir(args, context):
    relative = args.get("path", ".")
    root, target = resolve(context, relative)
    if target == root and not root.exists():
        return "(empty)"
    if not target.is_dir():
        raise ToolError(f"No folder named '{relative}' in the workspace.")
    names = sorted(p.name + ("/" if p.is_dir() else "") for p in target.iterdir())
    return "\n".join(names) or "(empty)"


def _existing_file(context, path):
    root, target = resolve(context, path)
    if not target.is_file():
        raise ToolError(f"No file named '{path}' in the workspace.")
    return root, target


def _read_text(target):
    with open(target, "rb") as file:
        data = file.read(MAX_READ_BYTES + 1)
    if len(data) > MAX_READ_BYTES:
        raise ToolError(f"The file is larger than {MAX_READ_BYTES:,} bytes.")
    if b"\x00" in data[:8192]:
        raise ToolError("This looks like a binary file, which read_file cannot show.")
    # Keep line endings as they are, so edit_file can match text exactly.
    return data.decode("utf-8-sig", errors="replace")


def read_file(args, context):
    root, target = _existing_file(context, args["path"])
    lines = _read_text(target).splitlines(keepends=True)
    offset = max(args.get("offset", 1), 1)
    limit = max(args.get("limit", DEFAULT_READ_LINES), 1)
    part = lines[offset - 1:offset - 1 + limit]
    text = "".join(part)
    if offset > 1 or offset - 1 + limit < len(lines):
        end = offset - 1 + len(part)
        text += (f"\n[Lines {offset}-{end} of {len(lines)}. Use offset and limit to read other lines.]"
                 if part else f"[The file has only {len(lines)} lines.]")
    return text or "(empty file)"


def write_file(args, context):
    root, target = resolve(context, args["path"])
    content = args["content"]
    if len(content) > MAX_WRITE_CHARS:
        raise ToolError(f"Content is limited to {MAX_WRITE_CHARS:,} characters per call.")
    if target.is_dir():
        raise ToolError(f"'{args['path']}' is a folder.")
    existed = target.exists()
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", encoding="utf-8", newline="") as file:
        file.write(content)
    return f"{'Replaced' if existed else 'Created'} {shown(root, target)} ({len(content):,} characters)."


def edit_file(args, context):
    root, target = _existing_file(context, args["path"])
    old, new = args["old_text"], args["new_text"]
    if not old:
        raise ToolError("old_text must not be empty.")
    text = _read_text(target)
    count = text.count(old)
    if count == 0:
        raise ToolError("old_text was not found. Read the file again and copy the text exactly.")
    if count > 1:
        raise ToolError(f"old_text appears {count} times. Include more surrounding text so it appears once.")
    with open(target, "w", encoding="utf-8", newline="") as file:
        file.write(text.replace(old, new, 1))
    return f"Edited {shown(root, target)}: replaced {len(old):,} characters with {len(new):,}."


def _schema(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


PATH = {"type": "string", "description": "Path relative to the workspace."}

FILE_TOOLS = [
    Tool("list_dir", "List files and folders in the user's workspace. Folder names end with '/'.",
         _schema({"path": {"type": "string", "description": "Folder in the workspace; default '.'."}}),
         "none", list_dir),
    Tool("read_file",
         f"Read a text file in the workspace. Returns up to {DEFAULT_READ_LINES} lines by default; use offset "
         "(first line, starting at 1) and limit for other parts of long files.",
         _schema({"path": PATH, "offset": {"type": "integer"}, "limit": {"type": "integer"}}, ["path"]),
         "none", read_file),
    Tool("write_file",
         "Create a file in the workspace, or replace all of an existing file's content. To change part of "
         "a file, use edit_file instead. The user is asked first.",
         _schema({"path": PATH, "content": {"type": "string"}}, ["path", "content"]),
         "local", write_file),
    Tool("edit_file",
         "Replace one piece of text in a workspace file. old_text must appear exactly once in the file; "
         "copy it exactly, including spaces and line breaks. The user is asked first.",
         _schema({"path": PATH, "old_text": {"type": "string"}, "new_text": {"type": "string"}},
                 ["path", "old_text", "new_text"]),
         "local", edit_file),
]


def default_tools(workspace, store=None):
    return ToolRegistry(FILE_TOOLS, ToolContext(workspace=workspace, store=store))
