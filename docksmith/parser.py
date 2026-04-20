"""
Docksmithfile parser.

Returns a list of instruction dicts:
  { "instruction": "RUN", "arg": "echo hi", "line": 3 }

Fails fast with a ValueError on any unknown instruction, including the
offending line number.
"""

KNOWN = {"FROM", "COPY", "RUN", "WORKDIR", "ENV", "CMD"}


def parse(content: str) -> list[dict]:
    instructions: list[dict] = []
    lines = content.splitlines()
    i = 0
    while i < len(lines):
        raw = lines[i]
        lineno = i + 1
        i += 1

        stripped = raw.strip()
        # skip blank lines and comments
        if not stripped or stripped.startswith("#"):
            continue

        # handle line-continuation backslash
        while stripped.endswith("\\"):
            stripped = stripped[:-1].rstrip()
            if i < len(lines):
                stripped += " " + lines[i].strip()
                i += 1

        parts = stripped.split(None, 1)
        if not parts:
            continue

        instr = parts[0].upper()
        arg = parts[1] if len(parts) > 1 else ""

        if instr not in KNOWN:
            raise ValueError(
                f"Unknown instruction '{parts[0]}' at line {lineno}: {stripped}"
            )

        instructions.append({"instruction": instr, "arg": arg, "line": lineno})

    return instructions
