"""Entry point for release builds: `pulse` / `pulse.exe` runs the same CLI as `uv run pulse`."""

from pulse.cli import main

if __name__ == "__main__":
    main()
