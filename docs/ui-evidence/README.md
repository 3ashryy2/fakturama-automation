# UIA evidence

Raw output from crawling the running Fakturama 2.2.0 UI Automation tree. These files are the measurements behind the design decisions in the top-level README and System Design Document.

- **`uia-tree-full-crawl.txt`** — every node reachable from the main window. Count the control types and the picture is stark: **1107 `PaneControl`, 18 `EditControl`, 2 `CheckBoxControl`, and zero `Table` / `DataGrid` / `DataItem` / `ListItem` nodes anywhere in the process.**

- **`uia-named-controls.txt`** — the subset that carries a usable `Name`. Essentially the toolbar, the menu bar and the navigation view. This is what the automation still drives through UIA; everything else needed another approach.

Two conclusions follow directly:

1. Grids are unreadable through accessibility, so grid state is established from rendered pixels and verified through the document's own native fields.
2. Form fields are unreachable through accessibility, so they are recovered from the native Win32 layer that SWT builds its widgets on.

Reproduce with `python -c "import grounding; print(grounding.Scope(grounding.main_window()).dump())"`.