# Contributing

Issues and pull requests are welcome.

## Before you open a pull request

1. Branch from `main` and keep each pull request to one change.
2. Run the unit tests from `scripts` (they need only the Python standard library and no running game):

   ```powershell
   cd scripts
   python -m unittest discover -p "test_*.py"
   ```

3. If you changed the C# mod, build it:

   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\build.ps1 -NoInstall
   ```

4. If you changed runtime behavior, check it in a running game (see
   [Getting Started](docs/guide/getting-started.md)) and describe what you saw in the pull request.
5. If you changed docs, run `powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\check-doc-links.ps1`.

## What a good change looks like

- Mutations go through the typed tools and the checkpoint → mutate → verify protocol; no raw game edits.
- No city-specific data (save names, coordinates, entity ids) in code or docs: the tools must work on any map.
- New behavior comes with unit tests.
